from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid1, uuid4

import httpx
import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from app.agent import cloud_trace
from app.agent.cloud_trace import (
    ClientAttemptRecord,
    CloudTraceConfig,
    CloudTraceExporter,
    client_attempt_observation_id,
    cloud_trace_id_for_run,
    load_cloud_trace_config,
)
from app.agent.telemetry import RunTrace, SpanRecord, SpanStatus, open_trace_store
from app.ai.messages import SystemMessage
from app.application.session.positions import now_ms
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    SendCommand,
    SendRequest,
)
from app.interfaces import http as http_module
from app.interfaces.http_api.client_attempts import (
    CACHE_CAPACITY,
    CACHE_TTL_SECONDS,
    MAX_CLIENT_DURATION_MS,
    RECEIPT_WINDOW_MS,
)
from test.regression_support import patch_default_database, patch_trace_database

START_MS = 1_760_000_000_000
FOREIGN_SCOPE = "other.instrumentation"


def make_config(**overrides: object) -> CloudTraceConfig:
    values: dict[str, object] = {
        "public_key": f"pk-lf-{uuid4().hex}",
        "secret_key": f"sk-lf-{uuid4().hex}",
        "base_url": "http://127.0.0.1:1",
        "environment": "development",
        "release": "fit-agent-test",
        "enabled": True,
        "queue_size": 1024,
        "close_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return CloudTraceConfig(**values)  # type: ignore[arg-type]


class Digest:
    def __init__(self, value: str) -> None:
        self._value = value

    def hexdigest(self) -> str:
        return self._value


class CapturingExporter(SpanExporter):
    def __init__(
        self,
        *,
        gate: threading.Event | None = None,
        delay: float = 0.0,
        fail_times: int = 0,
    ) -> None:
        self.spans: list = []
        self.shutdown_called = False
        self.entered = threading.Event()
        self.attempts = 0
        self._gate = gate
        self._delay = delay
        self._fail_times = fail_times
        self._lock = threading.Lock()

    def export(self, spans) -> SpanExportResult:
        with self._lock:
            self.attempts += 1
        self.entered.set()
        if self._gate is not None:
            self._gate.wait(10)
        if self._delay:
            time.sleep(self._delay)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("模拟导出失败")
        with self._lock:
            self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_called = True


def build_exporter(
    capture: SpanExporter, **overrides: object
) -> CloudTraceExporter:
    exporter = CloudTraceExporter.create(make_config(**overrides), span_exporter=capture)
    assert exporter is not None
    return exporter


def close_and_capture(
    exporter: CloudTraceExporter, capture: CapturingExporter
) -> list:
    exporter.close(5.0)
    return list(capture.spans)


def record(
    *,
    span_id: int,
    parent_span_id: int | None,
    name: str,
    trace_id: str,
    session_id: str,
    operation_id: str,
    run_id: str | None,
    started_at: int = START_MS,
    finished_at: int | None = None,
    duration_ms: float = 1000.0,
    status: str = "completed",
    error_code: str | None = None,
    attributes: dict | None = None,
) -> SpanRecord:
    return SpanRecord(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        session_id=session_id,
        run_id=run_id,
        operation_id=operation_id,
        name=name,
        started_at=started_at,
        finished_at=started_at + 1000 if finished_at is None else finished_at,
        duration_ms=duration_ms,
        status=status,
        error_code=error_code,
        attributes=attributes or {},
    )


def agent_snapshot(
    run_id: str, *, usage: dict | None = None, session_id: str | None = None
) -> list[SpanRecord]:
    trace_id = uuid4().hex
    session = session_id or str(uuid4())
    operation = str(uuid4())
    model_usage = {
        "provider": "openai-compatible",
        "model": "chat-model-real-id",
        "call_kind": "normal",
        "stop_reason": "stop",
        "first_text_ms": 12.5,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 2,
    }
    if usage is not None:
        model_usage.update(usage)
    common = {
        "trace_id": trace_id,
        "session_id": session,
        "operation_id": operation,
        "run_id": run_id,
    }
    return [
        record(span_id=1, parent_span_id=None, name="agent_run", **common,
               attributes={"first_text_ms": 20.0}),
        record(span_id=2, parent_span_id=1, name="turn", **common),
        record(span_id=3, parent_span_id=2, name="model", **common,
               attributes=model_usage),
        record(span_id=4, parent_span_id=2, name="tool", **common,
               attributes={
                   "tool_call_id": "call-1",
                   "tool_name": "read_file",
                   "execution_ms": 3.5,
                   "postprocess_ms": 1.5,
               }),
        record(span_id=5, parent_span_id=1, name="compaction", **common,
               attributes={"reason": "threshold"}),
    ]


def expected_span_id(identity: str, span_id: int) -> str:
    return sha256(f"fit-agent/business/{identity}/{span_id}".encode("utf-8")).hexdigest()[:16]


def by_name(spans: list) -> dict:
    return {span.name: span for span in spans}


def metadata_of(span) -> dict:
    return json.loads(dict(span.attributes)["langfuse.trace.metadata"])


def trace_ids(spans: list) -> set[str]:
    return {format(span.context.trace_id, "032x") for span in spans}


def wait_for_attempts(capture: CapturingExporter, count: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while capture.attempts < count:
        assert time.monotonic() < deadline, "导出调用未发生"
        time.sleep(0.01)


def test_disabled_switch_creates_no_client(monkeypatch, caplog):
    constructed: list = []

    def forbidden(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("关闭状态不得创建 Cloud 客户端")

    monkeypatch.setattr(cloud_trace, "Langfuse", forbidden)
    with caplog.at_level(logging.DEBUG, logger="app.agent.cloud_trace"):
        config = load_cloud_trace_config(
            {
                "FIT_TRACE_CLOUD_ENABLED": "false",
                "LANGFUSE_PUBLIC_KEY": "",
                "LANGFUSE_SECRET_KEY": "",
                "LANGFUSE_BASE_URL": "",
                "FIT_TRACE_ENVIRONMENT": "",
            }
        )
        assert config.enabled is False
        assert CloudTraceExporter.create(config) is None
        # 未设置开关时同样默认关闭。
        assert load_cloud_trace_config({}).enabled is False
    assert constructed == []


def test_invalid_switch_value_and_missing_credentials_are_diagnosed(monkeypatch, caplog):
    constructed: list = []

    def forbidden(*args, **kwargs):
        constructed.append(args)
        raise AssertionError("配置不完整不得创建客户端")

    monkeypatch.setattr(cloud_trace, "Langfuse", forbidden)
    with caplog.at_level(logging.INFO, logger="app.agent.cloud_trace"):
        invalid = load_cloud_trace_config({"FIT_TRACE_CLOUD_ENABLED": "maybe"})
        assert invalid.enabled is False
        missing = load_cloud_trace_config(
            {"FIT_TRACE_CLOUD_ENABLED": "true", "FIT_TRACE_ENVIRONMENT": "development"}
        )
        assert missing.enabled is True
        assert CloudTraceExporter.create(missing) is None
        bad_environment = make_config(environment="staging")
        assert CloudTraceExporter.create(bad_environment) is None
        assert constructed == []
        # 初始化抛错同样收敛为 None，业务按原有观测隔离继续。
        assert CloudTraceExporter.create(make_config()) is None
    assert len(constructed) == 1
    messages = [item.getMessage() for item in caplog.records]
    assert any("enabled_flag_invalid" in message for message in messages)
    assert any("missing_public_key" in message for message in messages)
    assert any("invalid_environment" in message for message in messages)
    assert any("AssertionError" in message for message in messages)


def test_unique_client_and_same_key_reuse():
    public_key = f"pk-lf-{uuid4().hex}"
    capture_a = CapturingExporter()
    capture_b = CapturingExporter()
    exporter_a = build_exporter(capture_a, public_key=public_key)
    exporter_b = build_exporter(capture_b, public_key=public_key)
    try:
        assert exporter_a._client._resources is exporter_b._client._resources
        exporter_a.submit(agent_snapshot(str(uuid4())))
        exporter_b.submit(agent_snapshot(str(uuid4())))
        spans_a = close_and_capture(exporter_a, capture_a)
        close_and_capture(exporter_b, capture_b)
        # 首个客户端持有唯一资源与 provider；后续同 key 的 provider 不被导出。
        assert spans_a
        assert capture_b.spans == []
    finally:
        exporter_a.close(5.0)
        exporter_b.close(5.0)


def test_foreign_scope_is_filtered():
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        foreign = exporter._provider.get_tracer(FOREIGN_SCOPE).start_span(
            "foreign", start_time=START_MS * 1_000_000
        )
        foreign.end(end_time=START_MS * 1_000_000 + 1_000_000)
        exporter.submit(agent_snapshot(str(uuid4())))
        spans = close_and_capture(exporter, capture)
        assert spans
        assert {span.instrumentation_scope.name for span in spans} == {
            "fit-agent.business"
        }
        assert "foreign" not in {span.name for span in spans}
    finally:
        exporter.close(5.0)


def test_snapshot_identity_times_types_and_attributes():
    run_id = str(uuid4())
    records = agent_snapshot(run_id)
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit(records)
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)

    assert {span.name for span in spans} == {
        "agent_run", "turn", "model", "tool", "compaction"
    }
    grouped = by_name(spans)
    trace_id = cloud_trace_id_for_run(run_id)
    assert {format(span.context.trace_id, "032x") for span in spans} == {trace_id}
    assert format(grouped["agent_run"].context.span_id, "016x") == expected_span_id(run_id, 1)
    assert format(grouped["model"].context.span_id, "016x") == expected_span_id(run_id, 3)
    assert grouped["agent_run"].parent is None
    assert format(grouped["turn"].parent.span_id, "016x") == expected_span_id(run_id, 1)
    assert format(grouped["model"].parent.span_id, "016x") == expected_span_id(run_id, 2)
    assert format(grouped["tool"].parent.span_id, "016x") == expected_span_id(run_id, 2)
    assert format(grouped["compaction"].parent.span_id, "016x") == expected_span_id(run_id, 1)

    assert grouped["agent_run"].start_time == START_MS * 1_000_000
    assert grouped["agent_run"].end_time == (START_MS + 1000) * 1_000_000

    root_attributes = dict(grouped["agent_run"].attributes)
    assert root_attributes["langfuse.trace.name"] == "agent_run"
    assert root_attributes["session.id"] == records[0].session_id
    assert root_attributes["langfuse.environment"] == "development"
    assert root_attributes["langfuse.release"] == "fit-agent-test"
    assert root_attributes["fit.source"] == "chat"
    assert root_attributes["fit.first_text_ms"] == 20.0
    assert root_attributes["fit.observation_origin"] == "server"
    assert root_attributes["fit.run_id"] == run_id
    assert root_attributes["fit.operation_id"] == records[0].operation_id

    model_attributes = dict(grouped["model"].attributes)
    assert model_attributes["langfuse.observation.type"] == "generation"
    assert model_attributes["langfuse.observation.model.name"] == "chat-model-real-id"
    usage = json.loads(model_attributes["langfuse.observation.usage_details"])
    assert usage == {
        "input": 10,
        "output": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 2,
        "total": 17,
    }
    assert model_attributes["fit.provider"] == "openai-compatible"
    assert model_attributes["fit.call_kind"] == "normal"
    assert model_attributes["fit.stop_reason"] == "stop"
    assert model_attributes["fit.first_text_ms"] == 12.5

    tool_attributes = dict(grouped["tool"].attributes)
    assert tool_attributes["langfuse.observation.type"] == "tool"
    assert tool_attributes["fit.tool_call_id"] == "call-1"
    assert tool_attributes["fit.tool_name"] == "read_file"
    assert tool_attributes["fit.execution_ms"] == 3.5
    assert tool_attributes["fit.postprocess_ms"] == 1.5

    assert dict(grouped["compaction"].attributes)["fit.reason"] == "threshold"
    assert "langfuse.observation.type" not in dict(grouped["turn"].attributes)
    assert all(list(span.events) == [] for span in spans)
    assert all(span.status.status_code == StatusCode.UNSET for span in spans)

    metadata = metadata_of(grouped["agent_run"])
    assert metadata == {
        "fit.source": "chat",
        "fit.usage.coverage": "full",
        "fit.usage.model_calls": 1,
    }


def test_partial_and_unknown_usage_coverage():
    partial_run = str(uuid4())
    partial = agent_snapshot(partial_run)
    partial.append(
        record(
            span_id=6,
            parent_span_id=2,
            name="model",
            trace_id=partial[0].trace_id,
            session_id=partial[0].session_id,
            operation_id=partial[0].operation_id,
            run_id=partial_run,
            attributes={
                "provider": "openai-compatible",
                "model": "summary-model-real-id",
                "call_kind": "history_summary",
                "input_tokens": 3,
                "output_tokens": 1,
            },
        )
    )
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit(partial)
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    grouped = by_name(spans)
    models = [span for span in spans if span.name == "model"]
    assert len(models) == 2
    summaries = [
        span for span in models
        if dict(span.attributes)["fit.call_kind"] == "history_summary"
    ]
    assert "langfuse.observation.usage_details" not in dict(summaries[0].attributes)
    assert metadata_of(grouped["agent_run"])["fit.usage.coverage"] == "partial"
    assert metadata_of(grouped["agent_run"])["fit.usage.model_calls"] == 2

    # 无模型调用的确认 Trace：覆盖率为 unknown，调用数为 0，且不写 priced_calls。
    operation_id = str(uuid4())
    confirmation = record(
        trace_id=uuid4().hex,
        span_id=1,
        parent_span_id=None,
        name="confirmation",
        session_id=str(uuid4()),
        operation_id=operation_id,
        run_id=None,
        attributes={"business_kind": "plan"},
    )
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit([confirmation])
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    root = by_name(spans)["confirmation"]
    assert format(root.context.trace_id, "032x") == Langfuse.create_trace_id(
        seed=f"fit-agent/business/confirmation/{operation_id}"
    )
    assert format(root.context.span_id, "016x") == expected_span_id(operation_id, 1)
    metadata = metadata_of(root)
    assert metadata == {"fit.usage.coverage": "unknown", "fit.usage.model_calls": 0}
    assert "fit.usage.priced_calls" not in metadata
    assert dict(root.attributes)["fit.business_kind"] == "plan"
    assert "fit.first_text_ms" not in dict(root.attributes)


def test_failed_and_cancelled_statuses_are_controlled():
    run_id = str(uuid4())
    records = agent_snapshot(run_id)
    records[2] = records[2].model_copy(
        update={"status": SpanStatus.failed, "error_code": "execution_failed"}
    )
    records[3] = records[3].model_copy(update={"status": SpanStatus.cancelled})
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit(records)
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    grouped = by_name(spans)
    model = grouped["model"]
    assert model.status.status_code == StatusCode.ERROR
    assert model.status.description == "execution_failed"
    assert dict(model.attributes)["langfuse.observation.level"] == "ERROR"
    assert dict(model.attributes)["langfuse.observation.status_message"] == "execution_failed"
    tool = grouped["tool"]
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.status.description == "cancelled"
    assert dict(tool.attributes)["langfuse.observation.level"] == "WARNING"
    assert dict(tool.attributes)["langfuse.observation.status_message"] == "cancelled"
    assert all(list(span.events) == [] for span in spans)


def test_clock_anomaly_uses_monotonic_duration():
    run_id = str(uuid4())
    records = agent_snapshot(run_id)
    records[2] = records[2].model_copy(
        update={"started_at": START_MS, "finished_at": START_MS - 5_000, "duration_ms": 250.0}
    )
    records[3] = records[3].model_copy(
        update={"started_at": START_MS, "finished_at": START_MS + 90_000, "duration_ms": 40.0}
    )
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit(records)
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    grouped = by_name(spans)
    model = grouped["model"]
    assert model.start_time == START_MS * 1_000_000
    assert model.end_time == START_MS * 1_000_000 + 250_000_000
    assert dict(model.attributes)["fit.clock_anomaly"] is True
    assert dict(model.attributes)["fit.clock_skew_ms"] == -5250.0
    tool = grouped["tool"]
    assert tool.end_time == START_MS * 1_000_000 + 40_000_000
    assert dict(tool.attributes)["fit.clock_anomaly"] is True
    assert dict(tool.attributes)["fit.clock_skew_ms"] > 1000


def test_span_id_collision_resolution_and_subtree_cull(monkeypatch):
    real = cloud_trace.sha256

    def colliding(material: bytes):
        text = material.decode("utf-8")
        if ":salt:" in text:
            return Digest("ab" * 32)
        return Digest("ab" * 32)

    monkeypatch.setattr(cloud_trace, "sha256", colliding)
    run_id = str(uuid4())
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit(agent_snapshot(run_id))
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    # 根取得固定 ID；其余 Span 全部碰撞后被剔除，其子树一并剔除。
    assert [span.name for span in spans] == ["agent_run"]

    def resalting(material: bytes):
        text = material.decode("utf-8")
        if ":salt:" in text:
            return real(material)
        return Digest("cd" * 32)

    monkeypatch.setattr(cloud_trace, "sha256", resalting)
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit(agent_snapshot(run_id))
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    assert {span.name for span in spans} == {
        "agent_run", "turn", "model", "tool", "compaction"
    }
    grouped = by_name(spans)
    assert format(grouped["agent_run"].context.span_id, "016x") == "cd" * 8
    assert format(grouped["turn"].context.span_id, "016x") == sha256(
        f"fit-agent/business/{run_id}/2:salt:1".encode("utf-8")
    ).hexdigest()[:16]


def test_id_plan_exhaustion_fails_instead_of_random_ids():
    from app.agent.cloud_trace import _StableIdGenerator

    generator = _StableIdGenerator()
    trace_id = cloud_trace_id_for_run(str(uuid4()))
    span_id = expected_span_id("probe", 1)
    with generator.plan(trace_id, [span_id]):
        assert generator.generate_trace_id() == int(trace_id, 16)
        assert generator.generate_span_id() == int(span_id, 16)
        with pytest.raises(RuntimeError):
            generator.generate_span_id()


def test_unplannable_root_aborts_snapshot_and_worker_continues(monkeypatch):
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    original = cloud_trace._plan_span_ids
    broken_run = str(uuid4())

    def selective(identity: str, records):
        return {} if identity == broken_run else original(identity, records)

    try:
        monkeypatch.setattr(cloud_trace, "_plan_span_ids", selective)
        exporter.submit(agent_snapshot(broken_run))
        healthy_run = str(uuid4())
        exporter.submit(agent_snapshot(healthy_run))
        spans = close_and_capture(exporter, capture)
    finally:
        monkeypatch.setattr(cloud_trace, "_plan_span_ids", original)
        exporter.close(5.0)
    assert {span.name for span in spans} == {
        "agent_run", "turn", "model", "tool", "compaction"
    }
    assert trace_ids(spans) == {cloud_trace_id_for_run(healthy_run)}


def test_parallel_tools_and_concurrent_runs_cross_thread():
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    traces = [agent_snapshot(str(uuid4())) for _ in range(6)]
    parallel = agent_snapshot(str(uuid4()))
    parallel.extend(
        [
            record(
                span_id=6,
                parent_span_id=2,
                name="tool",
                trace_id=parallel[0].trace_id,
                session_id=parallel[0].session_id,
                operation_id=parallel[0].operation_id,
                run_id=parallel[0].run_id,
                attributes={
                    "tool_call_id": "call-2",
                    "tool_name": "read_file",
                    "execution_ms": 2.0,
                },
            ),
            record(
                span_id=7,
                parent_span_id=6,
                name="model",
                trace_id=parallel[0].trace_id,
                session_id=parallel[0].session_id,
                operation_id=parallel[0].operation_id,
                run_id=parallel[0].run_id,
                attributes={
                    "provider": "openai-compatible",
                    "model": "nested-model-real-id",
                    "call_kind": "turn_prefix_summary",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            ),
        ]
    )
    traces.append(parallel)

    def submit(batch):
        exporter.submit(batch)

    threads = [threading.Thread(target=submit, args=(batch,)) for batch in traces]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    spans = close_and_capture(exporter, capture)
    exporter.close(5.0)

    assert trace_ids(spans) == {
        cloud_trace_id_for_run(batch[0].run_id) for batch in traces
    }
    parallel_spans = [
        span for span in spans
        if format(span.context.trace_id, "032x") == cloud_trace_id_for_run(parallel[0].run_id)
    ]
    assert len(parallel_spans) == 7
    tools = [span for span in parallel_spans if span.name == "tool"]
    assert {format(span.parent.span_id, "016x") for span in tools} == {
        expected_span_id(parallel[0].run_id, 2)
    }
    nested = next(
        span for span in parallel_spans
        if span.name == "model"
        and format(span.context.span_id, "016x") == expected_span_id(parallel[0].run_id, 7)
    )
    assert format(nested.parent.span_id, "016x") == expected_span_id(parallel[0].run_id, 6)


async def test_run_trace_finish_writes_local_and_cloud_together(tmp_path: Path):
    run_id = str(uuid4())
    store = await open_trace_store(tmp_path / "trace.db")
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        trace = RunTrace(
            session_id=str(uuid4()), operation_id=str(uuid4()), run_id=run_id,
            accepted_at=time.time_ns() // 1_000_000, accepted_perf=time.perf_counter(),
        )

        def root_body(root):
            return root.start_span(
                "turn", {}, lambda turn: turn.start_span(
                    "model", {
                        "provider": "openai-compatible",
                        "model": "chat-model-real-id",
                        "call_kind": "normal",
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                    },
                    lambda model: "ok",
                )
            )

        assert trace.start_span("agent_run", {}, root_body) == "ok"
        await trace.finish(store, exporter)
        spans = close_and_capture(exporter, capture)
        assert trace.released is True
        assert len(await store.read_trace(trace.trace_id)) == 3
        assert {span.name for span in spans} == {"agent_run", "turn", "model"}
        assert "client_attempt" not in {
            row.name.value for row in await store.read_trace(trace.trace_id)
        }
    finally:
        exporter.close(5.0)
        await store.close()


async def test_local_store_failure_still_exports_and_cloud_failure_is_isolated(tmp_path: Path):
    class BrokenStore:
        async def save(self, records):
            raise RuntimeError("本地观测写入失败")

    run_id = str(uuid4())
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        trace = RunTrace(session_id=str(uuid4()), operation_id=str(uuid4()), run_id=run_id)
        trace.start_span("agent_run", {}, lambda root: None)
        await trace.finish(BrokenStore(), exporter)
        spans = close_and_capture(exporter, capture)
        assert [span.name for span in spans] == ["agent_run"]
    finally:
        exporter.close(5.0)

    run_id = str(uuid4())
    store = await open_trace_store(tmp_path / "trace2.db")
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        trace = RunTrace(session_id=str(uuid4()), operation_id=str(uuid4()), run_id=run_id)
        trace.start_span("agent_run", {}, lambda root: None)
        await trace.finish(store, exporter)
        assert len(await store.read_trace(trace.trace_id)) == 1
        assert [span.name for span in close_and_capture(exporter, capture)] == ["agent_run"]
    finally:
        exporter.close(5.0)
        await store.close()

    class ExplodingSink:
        def submit(self, records):
            raise RuntimeError("Cloud 入队失败")

        def close(self, timeout_seconds=5.0):
            return None

    store = await open_trace_store(tmp_path / "trace3.db")
    try:
        trace = RunTrace(session_id=str(uuid4()), operation_id=str(uuid4()), run_id=str(uuid4()))
        trace.start_span("agent_run", {}, lambda root: None)
        await trace.finish(store, ExplodingSink())
        assert trace.released is True
        assert len(await store.read_trace(trace.trace_id)) == 1
    finally:
        await store.close()


def test_cloud_export_failure_does_not_stop_later_batches(monkeypatch):
    # 缩短 SDK 批次间隔，让每次提交形成独立的导出尝试。
    monkeypatch.setenv("LANGFUSE_FLUSH_INTERVAL", "0.05")
    capture = CapturingExporter(fail_times=2)
    exporter = build_exporter(capture)
    try:
        exporter.submit(agent_snapshot(str(uuid4())))
        wait_for_attempts(capture, 1)
        exporter.submit(agent_snapshot(str(uuid4())))
        wait_for_attempts(capture, 2)
        healthy = str(uuid4())
        exporter.submit(agent_snapshot(healthy))
        wait_for_attempts(capture, 3)
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    # 导出失败由 SDK 与适配器共同隔离：失败批次缺样，后续批次继续导出。
    assert trace_ids(spans) == {cloud_trace_id_for_run(healthy)}
    assert capture.shutdown_called is True


def test_queue_full_drops_whole_trace():
    gate = threading.Event()
    capture = CapturingExporter(gate=gate)
    exporter = build_exporter(capture, queue_size=1)
    first, second, third = (agent_snapshot(str(uuid4())) for _ in range(3))
    try:
        exporter.submit(first)
        assert capture.entered.wait(5)
        exporter.submit(second)
        exporter.submit(third)
        assert exporter.dropped_cloud_queue_full == 1
        gate.set()
        spans = close_and_capture(exporter, capture)
    finally:
        gate.set()
        exporter.close(5.0)
    exported = trace_ids(spans)
    assert cloud_trace_id_for_run(first[0].run_id) in exported
    assert cloud_trace_id_for_run(second[0].run_id) in exported
    assert cloud_trace_id_for_run(third[0].run_id) not in exported
    # 队满丢弃整条 Trace：被丢弃批次不产生任何 Span。
    assert len([span for span in spans if span.name == "model"]) == 2


def test_bounded_close_counts_timeout_and_releases_resources():
    capture = CapturingExporter(delay=0.8)
    exporter = build_exporter(capture)
    exporter.submit(agent_snapshot(str(uuid4())))
    started = time.monotonic()
    exporter.close(0.1)
    elapsed = time.monotonic() - started
    assert elapsed < 0.7
    assert exporter.dropped_cloud_close_timeout == 1
    exporter._thread.join(10)
    assert not exporter._thread.is_alive()
    assert capture.shutdown_called is True

    # 正常关闭：排空、释放导出器，关闭后的提交不再入队。
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    run_id = str(uuid4())
    exporter.submit(agent_snapshot(run_id))
    exporter.close(5.0)
    assert trace_ids(capture.spans) == {cloud_trace_id_for_run(run_id)}
    assert capture.shutdown_called is True
    assert exporter.dropped_cloud_close_timeout == 0
    exporter.submit(agent_snapshot(str(uuid4())))
    assert exporter.dropped_cloud_queue_full == 0


def test_close_race_with_submitters():
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    stop = threading.Event()

    def submitter():
        while not stop.is_set():
            exporter.submit(agent_snapshot(str(uuid4())))

    thread = threading.Thread(target=submitter)
    thread.start()
    try:
        exporter.close(5.0)
    finally:
        stop.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not exporter._thread.is_alive()


def test_privacy_surface_and_credentials(caplog):
    config = make_config()
    capture = CapturingExporter()
    with caplog.at_level(logging.DEBUG):
        exporter = CloudTraceExporter.create(config, span_exporter=capture)
        assert exporter is not None
        try:
            exporter.submit(agent_snapshot(str(uuid4())))
            spans = close_and_capture(exporter, capture)
        finally:
            exporter.close(5.0)
    assert config.secret_key not in caplog.text

    allowed_prefixes = (
        "langfuse.",
        "session.id",
        "fit.",
    )
    for span in spans:
        for key in dict(span.attributes):
            assert key.startswith(allowed_prefixes), key
        assert list(span.events) == []
        for value in dict(span.attributes).values():
            text = value if isinstance(value, str) else str(value)
            assert config.secret_key not in text
            assert config.public_key not in text
    # 模型输入输出与自由文本不进入 Cloud：模型 Span 只保留白名单字段。
    model_keys = set(dict(by_name(spans)["model"].attributes))
    assert model_keys == {
        "fit.observation_origin",
        "fit.session_id",
        "fit.operation_id",
        "fit.run_id",
        "langfuse.observation.type",
        "langfuse.observation.model.name",
        "langfuse.observation.usage_details",
        "fit.provider",
        "fit.call_kind",
        "fit.stop_reason",
        "fit.first_text_ms",
        "langfuse.observation.level",
    }


def test_client_attempt_node_joins_business_trace():
    run_id = str(uuid4())
    attempt = ClientAttemptRecord(
        session_id=str(uuid4()),
        operation_id=str(uuid4()),
        run_id=run_id,
        client_attempt_id=str(uuid4()),
        attempt_kind="send",
        client_duration_ms=1234.5,
        client_first_text_ms=456.75,
        client_terminal_kind="completed",
        received_at_ms=START_MS + 5000,
        processing_duration_ms=2.5,
    )
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit_client_attempt(
            attempt, trace_id=cloud_trace_id_for_run(run_id), parent_span_id=1
        )
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    assert len(spans) == 1
    node = spans[0]
    assert node.name == "client_attempt"
    assert format(node.context.trace_id, "032x") == cloud_trace_id_for_run(run_id)
    assert format(node.parent.span_id, "016x") == expected_span_id(run_id, 1)
    assert format(node.context.span_id, "016x") == sha256(
        f"fit-agent/client-attempt/{run_id}/{attempt.client_attempt_id}".encode("utf-8")
    ).hexdigest()[:16]
    assert node.start_time == (START_MS + 5000) * 1_000_000
    assert node.end_time == (START_MS + 5000) * 1_000_000 + 2_500_000
    attributes = dict(node.attributes)
    assert attributes["fit.observation_origin"] == "client"
    assert attributes["fit.client_attempt_id"] == attempt.client_attempt_id
    assert attributes["fit.attempt_kind"] == "send"
    assert attributes["fit.client_duration_ms"] == 1234.5
    assert attributes["fit.client_first_text_ms"] == 456.75
    assert attributes["fit.client_terminal_kind"] == "completed"
    assert attributes["fit.run_id"] == run_id
    assert attributes["fit.operation_id"] == attempt.operation_id
    assert attributes["fit.session_id"] == attempt.session_id
    assert attributes["langfuse.observation.level"] == "DEFAULT"
    assert node.status.status_code == StatusCode.UNSET
    assert list(node.events) == []
    assert "langfuse.observation.type" not in attributes

    missing = ClientAttemptRecord(
        session_id=attempt.session_id,
        operation_id=attempt.operation_id,
        run_id=run_id,
        client_attempt_id=str(uuid4()),
        attempt_kind="send",
        client_duration_ms=10.0,
        client_first_text_ms=None,
        client_terminal_kind="disconnected",
        received_at_ms=START_MS,
        processing_duration_ms=0.5,
    )
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit_client_attempt(
            missing, trace_id=cloud_trace_id_for_run(run_id), parent_span_id=1
        )
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    missing_attributes = dict(spans[0].attributes)
    assert missing_attributes["fit.client_duration_ms"] == 10.0
    assert "fit.client_first_text_ms" not in missing_attributes

    # 缺失完成时长时省略该属性；独立首正文保留真实零值。
    partial = ClientAttemptRecord(
        session_id=attempt.session_id,
        operation_id=attempt.operation_id,
        run_id=run_id,
        client_attempt_id=str(uuid4()),
        attempt_kind="send",
        client_duration_ms=None,
        client_first_text_ms=0.0,
        client_terminal_kind="unknown",
        received_at_ms=START_MS,
        processing_duration_ms=0.5,
    )
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit_client_attempt(
            partial, trace_id=cloud_trace_id_for_run(run_id), parent_span_id=1
        )
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    partial_attributes = dict(spans[0].attributes)
    assert "fit.client_duration_ms" not in partial_attributes
    assert partial_attributes["fit.client_first_text_ms"] == 0.0


def test_submit_detaches_snapshot_from_caller():
    run_id = str(uuid4())
    batch = agent_snapshot(run_id)
    assert SpanRecord.model_config.get("frozen") is True
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        exporter.submit(batch)
        batch.clear()
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
    assert len(spans) == 5


async def test_confirmation_service_exports_independent_trace(tmp_path: Path):
    from test.check_trace_confirmation import PROFILE, Fixture

    fixture = await Fixture(tmp_path, "cloud-confirmation").open()
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    try:
        session = await fixture.session()
        ids = await fixture.stage(
            session,
            "profile",
            {"profile_id": 1, "base_profile_version": None, "payload": PROFILE},
            "prepare_profile_update",
        )
        request = fixture.request(session, "profile", ids)
        operation = await fixture.confirmations.confirm(
            session, request, trace_store=fixture.store, sink=exporter
        )
        assert operation.status == "saved"
        # 同键复用不建立新记录器或根，也不重复导出。
        replay = await fixture.confirmations.confirm(
            session, request, trace_store=fixture.store, sink=exporter
        )
        assert replay.operation_id == operation.operation_id
        assert await fixture.store.trace_count() == 1
        spans = close_and_capture(exporter, capture)
    finally:
        exporter.close(5.0)
        await fixture.close()

    assert len(spans) == 1
    root = spans[0]
    assert root.name == "confirmation"
    assert format(root.context.trace_id, "032x") == Langfuse.create_trace_id(
        seed=f"fit-agent/business/confirmation/{operation.operation_id}"
    )
    attributes = dict(root.attributes)
    assert attributes["fit.business_kind"] == "profile"
    assert attributes["langfuse.trace.name"] == "confirmation"
    assert attributes["session.id"] == session
    assert attributes["fit.operation_id"] == operation.operation_id
    assert "fit.run_id" not in attributes
    assert root.status.status_code == StatusCode.UNSET


def test_client_attempt_rejects_invalid_trace_id():
    capture = CapturingExporter()
    exporter = build_exporter(capture)
    attempt = ClientAttemptRecord(
        session_id=str(uuid4()),
        operation_id=str(uuid4()),
        run_id=str(uuid4()),
        client_attempt_id=str(uuid4()),
        attempt_kind="send",
        client_duration_ms=1.0,
        client_first_text_ms=None,
        client_terminal_kind="unknown",
        received_at_ms=START_MS,
        processing_duration_ms=0.5,
    )
    try:
        with pytest.raises(ValueError):
            exporter.submit_client_attempt(attempt, trace_id="0" * 32, parent_span_id=1)
        with pytest.raises(ValueError):
            exporter.submit_client_attempt(
                attempt, trace_id=cloud_trace_id_for_run(attempt.run_id), parent_span_id=0
            )
    finally:
        exporter.close(5.0)


def test_lifespan_initializes_and_closes_real_exporter(monkeypatch):
    from app.interfaces import http as http_module

    patch_default_database("cloud-trace-real-lifespan")
    patch_trace_database("cloud-trace-real-lifespan")
    monkeypatch.setenv("FIT_TRACE_CLOUD_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", f"pk-lf-{uuid4().hex}")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", f"sk-lf-{uuid4().hex}")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FIT_TRACE_ENVIRONMENT", "development")
    monkeypatch.setenv("FIT_TRACE_RELEASE", "lifespan-probe")

    async def scenario():
        async with http_module.lifespan(http_module.app):
            exporter = http_module.app.state.cloud_trace
            assert isinstance(exporter, CloudTraceExporter)
            assert exporter._config.enabled is True
            assert exporter._config.environment == "development"
            return exporter

    exporter = asyncio.run(scenario())
    assert not exporter._thread.is_alive()


def test_lifespan_disabled_and_enabled_wiring(monkeypatch):
    from app.application.session.runtime import accepting_runs
    from app.interfaces import http as http_module

    patch_default_database("cloud-trace-lifespan")
    patch_trace_database("cloud-trace-lifespan")
    for name in (
        "FIT_TRACE_CLOUD_ENABLED",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "FIT_TRACE_ENVIRONMENT",
        "FIT_TRACE_RELEASE",
    ):
        monkeypatch.delenv(name, raising=False)

    async def disabled_lifespan():
        async with http_module.lifespan(http_module.app):
            assert http_module.app.state.cloud_trace is None

    asyncio.run(disabled_lifespan())

    events: list[str] = []

    class FakeSink:
        close_timeout_seconds = 0.5
        dropped_cloud_queue_full = 0
        dropped_cloud_close_timeout = 0

        def submit(self, records):
            events.append("submit")

        def submit_client_attempt(self, attempt, *, trace_id, parent_span_id):
            events.append("client_attempt")

        def close(self, timeout_seconds=5.0):
            events.append("close")

    class FakeExporter:
        @classmethod
        def create(cls, config, *, span_exporter=None):
            assert accepting_runs.is_set() is False
            assert config.enabled is True
            assert config.environment == "production"
            events.append("create")
            return FakeSink()

    monkeypatch.setenv("FIT_TRACE_CLOUD_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-lifespan")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-lifespan")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FIT_TRACE_ENVIRONMENT", "production")
    monkeypatch.setattr(http_module, "CloudTraceExporter", FakeExporter)

    async def enabled_lifespan():
        async with http_module.lifespan(http_module.app):
            assert isinstance(http_module.app.state.cloud_trace, FakeSink)

    asyncio.run(enabled_lifespan())
    assert events == ["create", "close"]


SYSTEM = SystemMessage(role="system", content="客户端观测专项", timestamp=1)
ATTEMPTS_PATH = "/api/agent/client-attempts"


def disable_cloud(monkeypatch) -> None:
    monkeypatch.setenv("FIT_TRACE_CLOUD_ENABLED", "false")


def attempts_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=http_module.app),
        base_url="http://127.0.0.1:8000",
        headers={"Origin": "http://localhost:5173"},
        timeout=10,
    )


async def accept_send(service, session: str, text: str = "开始"):
    await service.create_session(session, "客户端观测")
    return await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session,
            request=SendRequest(text=text),
        ),
        system_message=SYSTEM,
    )


def attempt_payload(
    session: str,
    operation: str,
    run_id: str | None,
    attempt_id: str | None = None,
    *,
    duration: float | None = 1234.5,
    first_text: float | None = 456.75,
    terminal: str = "completed",
    kind: str = "send",
    extra: dict | None = None,
) -> dict:
    body = {
        "session_id": session,
        "operation_id": operation,
        "run_id": run_id,
        "client_attempt_id": attempt_id or str(uuid4()),
        "attempt_kind": kind,
        "client_terminal_kind": terminal,
    }
    if duration is not None:
        body["client_duration_ms"] = duration
    if first_text is not None:
        body["client_first_text_ms"] = first_text
    if extra is not None:
        body.update(extra)
    return body


def assert_detail(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["code"] == code


async def raw_attempt_post(
    chunks: list[bytes], content_length: int | None
) -> list[dict]:
    pending = list(chunks)
    messages: list[dict] = []

    async def receive():
        if pending:
            body = pending.pop(0)
            return {
                "type": "http.request", "body": body, "more_body": bool(pending)
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"origin", b"http://localhost:5173"),
        (b"content-type", b"application/json"),
    ]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 12345),
        "scheme": "http", "method": "POST", "root_path": "",
        "path": ATTEMPTS_PATH, "raw_path": ATTEMPTS_PATH.encode(),
        "query_string": b"", "headers": headers,
    }
    await http_module.app(scope, receive, send)
    return messages


def test_client_attempt_endpoint_contract(monkeypatch):
    disable_cloud(monkeypatch)
    patch_default_database("client-attempt-contract")
    patch_trace_database("client-attempt-contract")

    async def scenario():
        async with http_module.lifespan(http_module.app):
            service = http_module.app.state.session_service
            repository = service._repository
            async with attempts_client() as client:
                session = str(uuid4())
                accepted = await accept_send(service, session)
                run = accepted.run
                running = attempt_payload(
                    session, accepted.operation.operation_id, run.id
                )
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=running),
                    409, "run_not_terminal",
                )

                unassociated = attempt_payload(session, str(uuid4()), None)
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=unassociated),
                    409, "attempt_unassociated",
                )
                wrong_run = attempt_payload(
                    session, accepted.operation.operation_id, str(uuid4())
                )
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=wrong_run),
                    409, "attempt_unassociated",
                )

                finished = await service.runs.finish_run(
                    session, run.id, "completed"
                )
                assert finished.run.finished_at is not None

                other = str(uuid4())
                other_send = await accept_send(service, other, "另一会话")
                await service.runs.finish_run(
                    other, other_send.run.id, "completed"
                )
                mismatched = attempt_payload(
                    session, other_send.operation.operation_id, other_send.run.id
                )
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=mismatched),
                    409, "attempt_unassociated",
                )

                edit_session = str(uuid4())
                edit_send = await accept_send(service, edit_session)
                await service.runs.finish_run(
                    edit_session, edit_send.run.id, "completed"
                )
                edit = await service.accept_edit(
                    EditCommand(
                        operation_id=str(uuid4()),
                        session_id=edit_session,
                        request=EditRequest(
                            target_entry_id=edit_send.run.request_entry_id,
                            text="编辑后内容",
                        ),
                    )
                )
                await service.runs.finish_run(
                    edit_session, edit.run.id, "completed"
                )
                edit_payload = attempt_payload(
                    edit_session, edit.operation.operation_id, edit.run.id
                )
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=edit_payload),
                    409, "attempt_unassociated",
                )

                attempt_id = str(uuid4())
                accepted_body = attempt_payload(
                    session, accepted.operation.operation_id, run.id, attempt_id
                )
                first = await client.post(ATTEMPTS_PATH, json=accepted_body)
                assert first.status_code == 200
                assert first.headers["cache-control"] == "no-store"
                assert first.json() == {
                    "status": "skipped",
                    "reason": "cloud_unavailable",
                    "observation_id": client_attempt_observation_id(
                        run.id, attempt_id
                    ),
                }
                duplicate = await client.post(
                    ATTEMPTS_PATH, json=accepted_body
                )
                assert duplicate.status_code == 200
                assert duplicate.json()["status"] == "duplicate"
                assert duplicate.json()["observation_id"] == first.json()[
                    "observation_id"
                ]

                conflict = await client.post(
                    ATTEMPTS_PATH,
                    json=attempt_payload(
                        session,
                        accepted.operation.operation_id,
                        run.id,
                        attempt_id,
                        terminal="failed",
                    ),
                )
                assert_detail(conflict, 409, "attempt_conflict")

                # 请求中的 null 与服务端解析出的真实 run_id 统一表达：同键仍为重复。
                null_run = attempt_payload(
                    session, accepted.operation.operation_id, None, attempt_id
                )
                null_duplicate = await client.post(
                    ATTEMPTS_PATH, json=null_run
                )
                assert null_duplicate.status_code == 200
                assert null_duplicate.json()["status"] == "duplicate"

                # 缺失测量直接省略；首正文可独立于完成时长存在，真实零值保留。
                partial = attempt_payload(
                    session,
                    accepted.operation.operation_id,
                    run.id,
                    duration=None,
                    first_text=0.0,
                    terminal="unknown",
                )
                partial_response = await client.post(
                    ATTEMPTS_PATH, json=partial
                )
                assert partial_response.status_code == 200
                assert partial_response.json()["status"] == "skipped"

                invalid = [
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        kind="edit",
                    ),
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        terminal="running",
                    ),
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        extra={"conversation": "正文"},
                    ),
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        duration=True,
                    ),
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        duration=-1,
                    ),
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        duration=MAX_CLIENT_DURATION_MS + 1,
                    ),
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        duration=10, first_text=11,
                    ),
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        attempt_id=str(uuid1()),
                    ),
                    attempt_payload(
                        session, accepted.operation.operation_id, run.id,
                        duration="10",
                    ),
                ]
                for payload in invalid:
                    assert_detail(
                        await client.post(ATTEMPTS_PATH, json=payload),
                        422, "invalid_request",
                    )

                zero = attempt_payload(
                    session, accepted.operation.operation_id, run.id,
                    duration=0.0, first_text=0.0,
                )
                zero_response = await client.post(ATTEMPTS_PATH, json=zero)
                assert zero_response.status_code == 200
                assert zero_response.json()["status"] == "skipped"

                interrupted_session = str(uuid4())
                interrupted = await accept_send(service, interrupted_session)
                await service.runs.finish_run(
                    interrupted_session, interrupted.run.id, "completed"
                )
                stored = await service.queries.require_run(
                    interrupted_session, interrupted.run.id
                )
                async with repository.transaction():
                    await repository.update_run(
                        stored.model_copy(update={"status": "interrupted"})
                    )
                interrupted_payload = attempt_payload(
                    interrupted_session, interrupted.operation.operation_id,
                    interrupted.run.id,
                )
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=interrupted_payload),
                    410, "attempt_unreportable",
                )

                unfinished_session = str(uuid4())
                unfinished = await accept_send(service, unfinished_session)
                await service.runs.finish_run(
                    unfinished_session, unfinished.run.id, "completed"
                )
                stored = await service.queries.require_run(
                    unfinished_session, unfinished.run.id
                )
                async with repository.transaction():
                    await repository.update_run(
                        stored.model_copy(update={"finished_at": None})
                    )
                unfinished_payload = attempt_payload(
                    unfinished_session, unfinished.operation.operation_id,
                    unfinished.run.id,
                )
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=unfinished_payload),
                    410, "attempt_unreportable",
                )

                expired_session = str(uuid4())
                expired = await accept_send(service, expired_session)
                await service.runs.finish_run(
                    expired_session, expired.run.id, "completed"
                )
                stored = await service.queries.require_run(
                    expired_session, expired.run.id
                )
                async with repository.transaction():
                    await repository.update_run(
                        stored.model_copy(
                            update={
                                "finished_at": (
                                    now_ms() - RECEIPT_WINDOW_MS - 1000
                                )
                            }
                        )
                    )
                expired_payload = attempt_payload(
                    expired_session, expired.operation.operation_id,
                    expired.run.id,
                )
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=expired_payload),
                    410, "attempt_expired",
                )
                async with repository.transaction():
                    await repository.update_run(
                        stored.model_copy(update={"finished_at": now_ms() + 5000})
                    )
                future_payload = attempt_payload(
                    expired_session, expired.operation.operation_id,
                    expired.run.id,
                )
                assert_detail(
                    await client.post(ATTEMPTS_PATH, json=future_payload),
                    410, "attempt_expired",
                )

                stored = await service.queries.require_run(session, run.id)
                assert stored.status == "completed"
                assert stored.finished_at == finished.run.finished_at

                # 4096 字节边界：恰好等于上限接受，超过一字节由逐块限制拒绝。
                padded = attempt_payload(
                    session, accepted.operation.operation_id, run.id
                )
                material = json.dumps(padded).encode("utf-8")
                exact = material + b" " * (4096 - len(material))
                assert len(exact) == 4096
                exact_response = await client.post(
                    ATTEMPTS_PATH, content=exact,
                    headers={"Content-Type": "application/json"},
                )
                assert exact_response.status_code == 200, exact_response.text
                oversized = material + b" " * (4097 - len(material))
                oversized_response = await client.post(
                    ATTEMPTS_PATH, content=oversized,
                    headers={"Content-Type": "application/json"},
                )
                assert_detail(oversized_response, 413, "request_size_exceeded")

                chunk = b" " * 3000
                missing_length = await raw_attempt_post([chunk, chunk], None)
                assert missing_length[0]["status"] == 413
                forged_length = await raw_attempt_post([chunk, chunk], 10)
                assert forged_length[0]["status"] == 413

    asyncio.run(scenario())


def test_client_attempt_endpoint_records_once_and_real_zero(monkeypatch):
    disable_cloud(monkeypatch)
    patch_default_database("client-attempt-recorded")
    patch_trace_database("client-attempt-recorded")

    async def scenario():
        capture = CapturingExporter()
        exporter = build_exporter(capture)
        async with http_module.lifespan(http_module.app):
            http_module.app.state.cloud_trace = exporter
            service = http_module.app.state.session_service
            session = str(uuid4())
            accepted = await accept_send(service, session)
            finished = await service.runs.finish_run(
                session, accepted.run.id, "completed"
            )
            attempt_id = str(uuid4())
            body = attempt_payload(
                session, accepted.operation.operation_id, accepted.run.id,
                attempt_id,
                duration=1234.5, first_text=456.75, terminal="completed",
            )
            async with attempts_client() as client:
                responses = await asyncio.gather(
                    *(client.post(ATTEMPTS_PATH, json=body) for _ in range(6))
                )
                statuses = sorted(response.status_code for response in responses)
                assert statuses == [200] * 5 + [202], [
                    response.text for response in responses
                ]
                recorded = next(
                    response for response in responses
                    if response.status_code == 202
                )
                observation_id = client_attempt_observation_id(
                    accepted.run.id, attempt_id
                )
                assert recorded.json() == {
                    "status": "recorded", "observation_id": observation_id
                }
                assert {
                    response.json()["observation_id"] for response in responses
                } == {observation_id}

                partial_id = str(uuid4())
                partial = attempt_payload(
                    session, accepted.operation.operation_id, accepted.run.id,
                    partial_id,
                    duration=None, first_text=0.0, terminal="unknown",
                )
                partial_response = await client.post(
                    ATTEMPTS_PATH, json=partial
                )
                assert partial_response.status_code == 202
                stored = await service.queries.require_run(
                    session, accepted.run.id
                )
                assert stored.status == "completed"
                assert stored.finished_at == finished.run.finished_at
        exporter.close(5.0)
        spans = list(capture.spans)
        assert {span.name for span in spans} == {"client_attempt"}
        assert len(spans) == 2
        by_attempt = {
            dict(span.attributes)["fit.client_attempt_id"]: span
            for span in spans
        }
        first = by_attempt[attempt_id]
        first_attributes = dict(first.attributes)
        assert first_attributes["fit.client_duration_ms"] == 1234.5
        assert first_attributes["fit.client_first_text_ms"] == 456.75
        assert first_attributes["fit.client_terminal_kind"] == "completed"
        assert format(first.context.trace_id, "032x") == cloud_trace_id_for_run(
            accepted.run.id
        )
        assert format(first.parent.span_id, "016x") == expected_span_id(
            accepted.run.id, 1
        )
        assert format(first.context.span_id, "016x") == observation_id
        second = by_attempt[partial_id]
        assert format(second.context.span_id, "016x") == (
            client_attempt_observation_id(accepted.run.id, partial_id)
        )
        second_attributes = dict(second.attributes)
        assert "fit.client_duration_ms" not in second_attributes
        assert second_attributes["fit.client_first_text_ms"] == 0.0
        assert second_attributes["fit.client_terminal_kind"] == "unknown"
        assert format(second.context.trace_id, "032x") == cloud_trace_id_for_run(
            accepted.run.id
        )
        assert format(second.parent.span_id, "016x") == expected_span_id(
            accepted.run.id, 1
        )

    asyncio.run(scenario())


def test_client_attempt_endpoint_isolates_submit_failure(monkeypatch):
    disable_cloud(monkeypatch)
    patch_default_database("client-attempt-failure")
    patch_trace_database("client-attempt-failure")

    class FailingSink:
        def submit_client_attempt(self, attempt, *, trace_id, parent_span_id):
            raise RuntimeError("模拟观测提交故障")

    async def scenario():
        capture = CapturingExporter()
        exporter = build_exporter(capture)
        async with http_module.lifespan(http_module.app):
            service = http_module.app.state.session_service
            session = str(uuid4())
            accepted = await accept_send(service, session)
            finished = await service.runs.finish_run(
                session, accepted.run.id, "completed"
            )
            body = attempt_payload(
                session, accepted.operation.operation_id, accepted.run.id
            )
            http_module.app.state.cloud_trace = FailingSink()
            async with attempts_client() as client:
                failed = await client.post(ATTEMPTS_PATH, json=body)
                assert_detail(failed, 503, "observation_unavailable")
                stored = await service.queries.require_run(
                    session, accepted.run.id
                )
                assert stored.status == "completed"
                assert stored.finished_at == finished.run.finished_at
                http_module.app.state.cloud_trace = exporter
                retried = await client.post(ATTEMPTS_PATH, json=body)
                assert retried.status_code == 202, retried.text
                assert retried.json()["status"] == "recorded"
        exporter.close(5.0)
        assert len(capture.spans) == 1
        assert capture.spans[0].name == "client_attempt"

    asyncio.run(scenario())


def test_client_attempt_cache_bounds_and_expiry():
    from app.interfaces.http_api.client_attempts import ClientAttemptCache

    assert CACHE_CAPACITY == 4096
    assert CACHE_TTL_SECONDS == 600.0
    cache = ClientAttemptCache(capacity=2, ttl_seconds=0.01)
    cache.put(("run", "a"), "one")
    cache.put(("run", "b"), "two")
    assert cache.classify(("run", "a"), "one") == "duplicate"
    assert cache.classify(("run", "a"), "other") == "conflict"
    cache.put(("run", "c"), "three")
    assert cache.classify(("run", "a"), "one") == "new"
    time.sleep(0.02)
    assert cache.classify(("run", "b"), "two") == "new"
