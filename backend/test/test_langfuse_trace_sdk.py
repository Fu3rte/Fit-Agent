import inspect
import json
import threading
import time
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

import langfuse
from langfuse import Langfuse
from opentelemetry import trace, version
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import Span, Status, StatusCode, Tracer

SCOPE = "fit-agent.business"
START_NS = 1_700_000_000_000_000_000


class CapturingExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans = []
        self.shutdown_called = False

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_called = True


class PlannedIdGenerator(IdGenerator):
    def __init__(self) -> None:
        self._local = threading.local()

    @contextmanager
    def plan(self, trace_id: str, span_ids):
        self._local.trace_id = int(trace_id, 16)
        self._local.span_ids = [int(span_id, 16) for span_id in span_ids]
        try:
            yield
        finally:
            self._local.trace_id = None
            self._local.span_ids = []

    def generate_trace_id(self) -> int:
        planned = getattr(self._local, "trace_id", None)
        if planned is not None:
            return planned
        return RandomIdGenerator().generate_trace_id()

    def generate_span_id(self) -> int:
        planned = getattr(self._local, "span_ids", [])
        if planned:
            return planned.pop(0)
        return RandomIdGenerator().generate_span_id()


def build_client(*, public_key: str | None = None, scope: str = SCOPE):
    capture = CapturingExporter()
    generator = PlannedIdGenerator()
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "fit-agent",
                "langfuse.environment": "test",
                "langfuse.release": "fit-agent-test",
            }
        ),
        id_generator=generator,
    )
    client = Langfuse(
        public_key=public_key or f"pk-lf-{uuid4().hex}",
        secret_key=f"sk-lf-{uuid4().hex}",
        base_url="http://127.0.0.1:1",
        tracer_provider=provider,
        span_exporter=capture,
        should_export_span=lambda span: span.instrumentation_scope.name == scope,
        environment="test",
        release="fit-agent-test",
    )
    return provider, client, generator, capture


def close_client(provider: TracerProvider, client: Langfuse) -> None:
    client.shutdown()
    provider.shutdown()


def span_hex(run_id: str, local_span_id: int) -> str:
    seed = f"fit-agent/business/{run_id}/{local_span_id}"
    return sha256(seed.encode("utf-8")).hexdigest()[:16]


def stable_span_id(seed: str, used: set[str]) -> str:
    for salt in range(8):
        material = seed if salt == 0 else f"{seed}:salt:{salt}"
        candidate = sha256(material.encode("utf-8")).hexdigest()[:16]
        if int(candidate, 16) != 0 and candidate not in used:
            used.add(candidate)
            return candidate
    raise ValueError("无法生成非零且不冲突的 Span ID")


def test_public_sdk_surface_supports_historical_times():
    assert langfuse.__version__ == "5.0.4"
    assert version.__version__ == "1.45.1"
    assert "tracer_provider" in inspect.signature(Langfuse.__init__).parameters
    assert not hasattr(Langfuse, "start_span")
    assert "start_time" not in inspect.signature(Langfuse.start_observation).parameters
    assert "start_time" in inspect.signature(Tracer.start_span).parameters
    assert "end_time" in inspect.signature(Span.end).parameters


def test_public_id_helpers_are_deterministic_and_nonzero():
    run_id = str(uuid4())
    seed = f"fit-agent/business/run/{run_id}"
    first = Langfuse.create_trace_id(seed=seed)
    second = Langfuse.create_trace_id(seed=seed)
    assert first == second
    assert len(first) == 32
    assert int(first, 16) != 0
    mapped = span_hex(run_id, 1)
    assert len(mapped) == 16
    assert int(mapped, 16) != 0


def test_stable_span_mapping_handles_collisions():
    seed = f"fit-agent/business/{uuid4()}/2"
    used: set[str] = set()
    first = stable_span_id(seed, used)
    assert len(first) == 16
    assert int(first, 16) != 0
    second = stable_span_id(seed, used)
    assert len(second) == 16
    assert int(second, 16) != 0
    assert first != second
    assert used == {first, second}


def test_historical_times_ids_parent_and_observation_types():
    run_id = str(uuid4())
    trace_id = Langfuse.create_trace_id(seed=f"fit-agent/business/run/{run_id}")
    root_id, model_id, tool_id = (span_hex(run_id, index) for index in (1, 2, 3))
    provider, client, generator, capture = build_client()
    try:
        tracer = provider.get_tracer(SCOPE, "1")
        with generator.plan(trace_id, [root_id, model_id, tool_id]):
            root = tracer.start_span(
                "agent_run",
                start_time=START_NS,
                attributes={
                    "langfuse.trace.name": "agent_run",
                    "langfuse.environment": "test",
                    "langfuse.release": "fit-agent-test",
                    "session.id": "22222222-2222-4222-8222-222222222222",
                },
            )
            model = tracer.start_span(
                "model",
                context=trace.set_span_in_context(root),
                start_time=START_NS + 1_000_000,
                attributes={
                    "langfuse.observation.type": "generation",
                    "langfuse.observation.model.name": "model-probe",
                    "langfuse.observation.usage_details": json.dumps(
                        {
                            "input": 10,
                            "output": 5,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 2,
                            "total": 17,
                        }
                    ),
                },
            )
            tool = tracer.start_span(
                "tool",
                context=trace.set_span_in_context(root),
                start_time=START_NS + 2_000_000,
                attributes={"langfuse.observation.type": "tool"},
            )
            tool.end(end_time=START_NS + 2_500_000)
            model.end(end_time=START_NS + 2_000_000)
            root.end(end_time=START_NS + 4_000_000)
        provider.force_flush()

        by_name = {span.name: span for span in capture.spans}
        assert set(by_name) == {"agent_run", "model", "tool"}
        assert format(by_name["agent_run"].context.trace_id, "032x") == trace_id
        assert format(by_name["model"].context.trace_id, "032x") == trace_id
        assert format(by_name["tool"].context.trace_id, "032x") == trace_id
        assert format(by_name["agent_run"].context.span_id, "016x") == root_id
        assert format(by_name["model"].context.span_id, "016x") == model_id
        assert format(by_name["tool"].context.span_id, "016x") == tool_id
        assert by_name["agent_run"].parent is None
        assert format(by_name["model"].parent.span_id, "016x") == root_id
        assert format(by_name["tool"].parent.span_id, "016x") == root_id
        assert by_name["agent_run"].start_time == START_NS
        assert by_name["agent_run"].end_time == START_NS + 4_000_000
        assert by_name["model"].start_time == START_NS + 1_000_000
        assert by_name["model"].end_time == START_NS + 2_000_000
        assert by_name["tool"].start_time == START_NS + 2_000_000
        assert by_name["tool"].end_time == START_NS + 2_500_000

        model_attributes = dict(by_name["model"].attributes)
        assert model_attributes["langfuse.observation.type"] == "generation"
        assert model_attributes["langfuse.observation.model.name"] == "model-probe"
        usage = json.loads(model_attributes["langfuse.observation.usage_details"])
        assert usage == {
            "input": 10,
            "output": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 2,
            "total": 17,
        }
        assert dict(by_name["tool"].attributes)["langfuse.observation.type"] == "tool"
        root_attributes = dict(by_name["agent_run"].attributes)
        assert root_attributes["langfuse.trace.name"] == "agent_run"
        assert root_attributes["langfuse.environment"] == "test"
        assert root_attributes["session.id"] == (
            "22222222-2222-4222-8222-222222222222"
        )
        for span in capture.spans:
            assert list(span.events) == []
            assert span.status.status_code == StatusCode.UNSET
    finally:
        close_client(provider, client)


def test_foreign_scope_is_filtered():
    provider, client, _generator, capture = build_client()
    try:
        span = provider.get_tracer("other.instrumentation").start_span(
            "foreign", start_time=START_NS
        )
        span.end(end_time=START_NS + 1_000_000)
        provider.force_flush()
        assert capture.spans == []
    finally:
        close_client(provider, client)


def test_error_status_keeps_controlled_detail_and_no_exception_event():
    provider, client, _generator, capture = build_client()
    try:
        span = provider.get_tracer(SCOPE).start_span(
            "tool",
            start_time=START_NS,
            record_exception=False,
            set_status_on_exception=False,
            attributes={
                "langfuse.observation.level": "ERROR",
                "langfuse.observation.status_message": "execution_failed",
            },
        )
        span.set_status(Status(StatusCode.ERROR, "execution_failed"))
        span.end(end_time=START_NS + 1_000_000)
        provider.force_flush()
        exported = capture.spans[0]
        assert exported.status.status_code == StatusCode.ERROR
        assert exported.status.description == "execution_failed"
        assert list(exported.events) == []
    finally:
        close_client(provider, client)


def test_same_public_key_reuses_first_provider():
    public_key = f"pk-lf-{uuid4().hex}"
    provider_a, client_a, _generator_a, capture_a = build_client(
        public_key=public_key
    )
    provider_b, _client_b, _generator_b, capture_b = build_client(
        public_key=public_key
    )
    try:
        provider_a.get_tracer(SCOPE).start_span("a", start_time=START_NS).end(
            end_time=START_NS + 1_000_000
        )
        provider_b.get_tracer(SCOPE).start_span("b", start_time=START_NS).end(
            end_time=START_NS + 1_000_000
        )
        provider_a.force_flush()
        provider_b.force_flush()
        assert [span.name for span in capture_a.spans] == ["a"]
        assert capture_b.spans == []
    finally:
        client_a.shutdown()
        provider_a.shutdown()
        provider_b.shutdown()


def test_shutdown_releases_exporter():
    provider, client, _generator, capture = build_client()
    provider.get_tracer(SCOPE).start_span("agent_run", start_time=START_NS).end(
        end_time=START_NS + 1_000_000
    )
    provider.force_flush()
    started = time.monotonic()
    client.shutdown()
    provider.shutdown()
    assert time.monotonic() - started < 10.0
    assert capture.shutdown_called is True
