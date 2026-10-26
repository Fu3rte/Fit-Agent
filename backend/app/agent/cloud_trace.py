import json
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from queue import Full, Queue
from threading import Lock, Thread
from time import monotonic
from typing import Literal, Protocol

from langfuse import Langfuse
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    Tracer,
)

from app.agent.telemetry import SpanName, SpanRecord

logger = logging.getLogger(__name__)

# 业务进程独占的 instrumentation scope；评测回放与网页裁判使用独立进程与独立 scope。
FIT_AGENT_TRACER_NAME = "fit-agent.business"
FIT_AGENT_TRACER_VERSION = "1"
FIT_TRACE_ENVIRONMENTS = frozenset({"production", "development"})
CLOUD_TRACE_QUEUE_SIZE = 1024
CLOUD_TRACE_CLOSE_TIMEOUT_SECONDS = 5.0

_MAX_ID_SALT = 8
_TRACE_ID_LENGTH = 32
_SPAN_ID_LENGTH = 16
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})
_DISABLED_VALUES = frozenset({"", "0", "false", "no", "off"})
_CLOCK_SKEW_TOLERANCE_MS = 1000

# 模型 Span 的四项互斥用量桶：子集字段不重复计入，total 只作汇总。
_USAGE_KEYS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_tokens", "cache_read_input_tokens"),
    ("cache_write_tokens", "cache_creation_input_tokens"),
)


@dataclass(frozen=True)
class CloudTraceConfig:
    public_key: str
    secret_key: str
    base_url: str
    environment: str
    release: str | None
    enabled: bool
    queue_size: int = CLOUD_TRACE_QUEUE_SIZE
    close_timeout_seconds: float = CLOUD_TRACE_CLOSE_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ClientAttemptRecord:
    session_id: str
    operation_id: str
    run_id: str
    client_attempt_id: str
    attempt_kind: Literal["send"]
    # 客户端测量允许缺失：只导出实际取得的数值，不用零值或推算值补齐。
    client_duration_ms: float | None
    client_first_text_ms: float | None
    client_terminal_kind: Literal[
        "completed", "failed", "cancelled", "disconnected", "unknown"
    ]
    received_at_ms: int
    processing_duration_ms: float


class SpanExportSink(Protocol):
    def submit(self, records: Sequence[SpanRecord]) -> None: ...

    def submit_client_attempt(
        self,
        attempt: ClientAttemptRecord,
        *,
        trace_id: str,
        parent_span_id: int,
    ) -> None: ...

    def close(self, timeout_seconds: float = CLOUD_TRACE_CLOSE_TIMEOUT_SECONDS) -> None: ...


@dataclass(frozen=True)
class _SnapshotJob:
    records: tuple[SpanRecord, ...]


@dataclass(frozen=True)
class _ClientAttemptJob:
    attempt: ClientAttemptRecord
    trace_id: str
    parent_span_id: int


def _flag(source: Mapping[str, str]) -> bool:
    value = source.get("FIT_TRACE_CLOUD_ENABLED", "").strip().lower()
    if value in _ENABLED_VALUES:
        return True
    if value in _DISABLED_VALUES:
        return False
    logger.error("Cloud Trace 配置错误（enabled_flag_invalid）")
    return False


def load_cloud_trace_config(
    environ: Mapping[str, str] | None = None,
) -> CloudTraceConfig:
    # 只读取服务端可信配置；关闭时保留空凭据，不校验可选 Cloud 参数。
    source = os.environ if environ is None else environ
    return CloudTraceConfig(
        public_key=source.get("LANGFUSE_PUBLIC_KEY", "").strip(),
        secret_key=source.get("LANGFUSE_SECRET_KEY", "").strip(),
        base_url=source.get("LANGFUSE_BASE_URL", "").strip(),
        environment=source.get("FIT_TRACE_ENVIRONMENT", "").strip(),
        release=(source.get("FIT_TRACE_RELEASE") or "").strip() or None,
        enabled=_flag(source),
    )


def _config_problem(config: CloudTraceConfig) -> str | None:
    if not config.public_key:
        return "missing_public_key"
    if not config.secret_key:
        return "missing_secret_key"
    if not config.base_url:
        return "missing_base_url"
    if config.environment not in FIT_TRACE_ENVIRONMENTS:
        return "invalid_environment"
    if config.queue_size < 1:
        return "invalid_queue_size"
    if not config.close_timeout_seconds > 0:
        return "invalid_close_timeout"
    return None


def _is_trace_id(value: str) -> bool:
    return (
        len(value) == _TRACE_ID_LENGTH
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
        and int(value, 16) != 0
    )


def _salted_hex(seed: str, used: set[str], length: int) -> str | None:
    # 计划身份必须非零且在给定集合内唯一：同一 seed 的重复导出得到同一结果。
    for salt in range(_MAX_ID_SALT):
        material = seed if salt == 0 else f"{seed}:salt:{salt}"
        candidate = sha256(material.encode("utf-8")).hexdigest()[:length]
        if int(candidate, 16) != 0 and candidate not in used:
            return candidate
    return None


def cloud_trace_id_for_run(run_id: str) -> str:
    # 任务 3 以该结果作为 client_attempt 的 trace_id；确认保存使用 operation_id 作为身份。
    return _cloud_trace_id(f"fit-agent/business/run/{run_id}")


def client_attempt_observation_id(run_id: str, client_attempt_id: str) -> str:
    # HTTP 回执与导出节点共用同一身份计算；同一运行的同一次传输尝试得到同一编号。
    identifier = _salted_hex(
        f"fit-agent/client-attempt/{run_id}/{client_attempt_id}",
        set(),
        _SPAN_ID_LENGTH,
    )
    if identifier is None:
        raise ValueError("无法生成 client_attempt 观测身份")
    return identifier


def _cloud_trace_id(seed: str) -> str:
    for salt in range(_MAX_ID_SALT):
        material = seed if salt == 0 else f"{seed}:salt:{salt}"
        candidate = Langfuse.create_trace_id(seed=material).lower()
        if _is_trace_id(candidate):
            return candidate
    raise ValueError("无法生成非零且格式合法的 Cloud Trace ID")


def _plan_span_ids(
    identity: str, records: Sequence[SpanRecord]
) -> dict[int, str]:
    # 同 Trace 内碰撞与零值重算；仍无法保持身份时剔除该 Span 及其子树。
    planned: dict[int, str] = {}
    rejected: set[int] = set()
    used: set[str] = set()
    for record in sorted(records, key=lambda item: item.span_id):
        if record.parent_span_id in rejected:
            rejected.add(record.span_id)
            continue
        resolved = _salted_hex(
            f"fit-agent/business/{identity}/{record.span_id}", used, _SPAN_ID_LENGTH
        )
        if resolved is None:
            rejected.add(record.span_id)
            continue
        used.add(resolved)
        planned[record.span_id] = resolved
    return planned


def _usage_details(attributes: Mapping[str, object]) -> dict[str, int] | None:
    # 四项任一未知即整组省略；四项齐备时零值有效。
    values: dict[str, int] = {}
    for source_key, bucket in _USAGE_KEYS:
        value = attributes.get(source_key)
        if value is None:
            return None
        values[bucket] = int(value)
    return {**values, "total": sum(values.values())}


def _usage_coverage(records: Sequence[SpanRecord]) -> str:
    models = [record for record in records if record.name is SpanName.model]
    complete = sum(1 for record in models if _usage_details(record.attributes) is not None)
    if not models or complete == 0:
        return "unknown"
    return "full" if complete == len(models) else "partial"


def _trace_metadata(records: Sequence[SpanRecord], root: SpanRecord) -> dict[str, object]:
    # 定价覆盖率由 Cloud 侧确定；本地只记录调用数与用量完整性，价格未核验前不写 priced_calls。
    metadata: dict[str, object] = {
        "fit.usage.coverage": _usage_coverage(records),
        "fit.usage.model_calls": sum(
            1 for record in records if record.name is SpanName.model
        ),
    }
    if root.name is SpanName.agent_run:
        metadata["fit.source"] = "chat"
    return metadata


def _record_times(record: SpanRecord) -> tuple[int, int, dict[str, object]]:
    # 历史起止按真实 UTC 毫秒转换；壁钟异常时回退到单调时长并留下标记。
    start_ns = record.started_at * 1_000_000
    wall_ms = record.finished_at - record.started_at
    skew_ms = wall_ms - record.duration_ms
    if wall_ms < 0 or abs(skew_ms) > _CLOCK_SKEW_TOLERANCE_MS:
        anomaly = {"fit.clock_anomaly": True, "fit.clock_skew_ms": skew_ms}
        return start_ns, start_ns + round(record.duration_ms * 1_000_000), anomaly
    return start_ns, record.finished_at * 1_000_000, {}


def _status_attributes(record: SpanRecord) -> tuple[dict[str, object], Status | None]:
    if record.status.value == "failed":
        message = record.error_code
        attributes: dict[str, object] = {"langfuse.observation.level": "ERROR"}
        if message is not None:
            attributes["langfuse.observation.status_message"] = message
        return attributes, Status(StatusCode.ERROR, message or "")
    if record.status.value == "cancelled":
        return (
            {
                "langfuse.observation.level": "WARNING",
                "langfuse.observation.status_message": "cancelled",
            },
            Status(StatusCode.ERROR, "cancelled"),
        )
    return {"langfuse.observation.level": "DEFAULT"}, None


def _server_attributes(
    record: SpanRecord,
    *,
    environment: str,
    release: str | None,
    metadata: dict[str, object],
) -> dict[str, object]:
    # 出口白名单：只写受控身份、名称、受控状态与契约列举的 fit.* / langfuse.* 字段。
    attributes: dict[str, object] = {
        "fit.observation_origin": "server",
        "fit.session_id": record.session_id,
        "fit.operation_id": record.operation_id,
    }
    if record.run_id is not None:
        attributes["fit.run_id"] = record.run_id
    source = record.attributes
    if record.name is SpanName.model:
        attributes["langfuse.observation.type"] = "generation"
        attributes["langfuse.observation.model.name"] = source["model"]
        usage = _usage_details(source)
        if usage is not None:
            attributes["langfuse.observation.usage_details"] = json.dumps(
                usage, ensure_ascii=False, allow_nan=False, sort_keys=True
            )
        for source_key, target_key in (
            ("provider", "fit.provider"),
            ("call_kind", "fit.call_kind"),
            ("stop_reason", "fit.stop_reason"),
            ("first_text_ms", "fit.first_text_ms"),
        ):
            if source_key in source:
                attributes[target_key] = source[source_key]
    elif record.name is SpanName.tool:
        attributes["langfuse.observation.type"] = "tool"
        for source_key, target_key in (
            ("tool_call_id", "fit.tool_call_id"),
            ("tool_name", "fit.tool_name"),
            ("execution_ms", "fit.execution_ms"),
            ("postprocess_ms", "fit.postprocess_ms"),
        ):
            if source_key in source:
                attributes[target_key] = source[source_key]
    elif record.name is SpanName.compaction:
        if "reason" in source:
            attributes["fit.reason"] = source["reason"]
    elif record.name is SpanName.confirmation:
        if "business_kind" in source:
            attributes["fit.business_kind"] = source["business_kind"]
    if record.parent_span_id is None:
        attributes["langfuse.trace.name"] = record.name.value
        attributes["session.id"] = record.session_id
        attributes["langfuse.environment"] = environment
        if release is not None:
            attributes["langfuse.release"] = release
        attributes["langfuse.trace.metadata"] = json.dumps(
            metadata, ensure_ascii=False, allow_nan=False, sort_keys=True
        )
        if record.name is SpanName.agent_run:
            attributes["fit.source"] = "chat"
            if "first_text_ms" in source:
                attributes["fit.first_text_ms"] = source["first_text_ms"]
    status_attributes, _status = _status_attributes(record)
    attributes.update(status_attributes)
    return attributes


def _attempt_attributes(attempt: ClientAttemptRecord) -> dict[str, object]:
    # 只输出已知测量；首正文与完成时长各自独立，缺失时省略而不是补零。
    attributes: dict[str, object] = {
        "fit.observation_origin": "client",
        "fit.session_id": attempt.session_id,
        "fit.run_id": attempt.run_id,
        "fit.operation_id": attempt.operation_id,
        "fit.client_attempt_id": attempt.client_attempt_id,
        "fit.attempt_kind": attempt.attempt_kind,
        "fit.client_terminal_kind": attempt.client_terminal_kind,
        "langfuse.observation.level": "DEFAULT",
    }
    if attempt.client_duration_ms is not None:
        attributes["fit.client_duration_ms"] = attempt.client_duration_ms
    if attempt.client_first_text_ms is not None:
        attributes["fit.client_first_text_ms"] = attempt.client_first_text_ms
    return attributes


def _should_export_span(span: ReadableSpan) -> bool:
    # 业务进程只导出自有 scope；其他库与未知 scope 精确丢弃。
    scope = span.instrumentation_scope
    return scope is not None and scope.name == FIT_AGENT_TRACER_NAME


class _StableIdGenerator(IdGenerator):
    # 计划只对导出线程生效：计划耗尽直接失败，禁止回退随机身份破坏关联。
    def __init__(self) -> None:
        self._local = _PlanState()

    @contextmanager
    def plan(self, trace_id: str, span_ids: Sequence[str]) -> Iterator[None]:
        previous = self._local.detach()
        self._local.attach(trace_id, span_ids)
        try:
            yield
        finally:
            self._local.restore(previous)

    def generate_trace_id(self) -> int:
        planned = self._local.take_trace_id()
        if planned is not None:
            return planned
        if self._local.planning:
            raise RuntimeError("Cloud Trace ID 计划耗尽")
        return RandomIdGenerator().generate_trace_id()

    def generate_span_id(self) -> int:
        planned = self._local.take_span_id()
        if planned is not None:
            return planned
        if self._local.planning:
            raise RuntimeError("Cloud Span ID 计划耗尽")
        return RandomIdGenerator().generate_span_id()


class _PlanState:
    # 线程局部计划：导出线程持有唯一一份，跨线程互不可见。
    __slots__ = ("trace_id", "span_ids", "planning")

    def __init__(self) -> None:
        self.trace_id: int | None = None
        self.span_ids: list[int] = []
        self.planning = False

    def attach(self, trace_id: str, span_ids: Sequence[str]) -> None:
        self.trace_id = int(trace_id, 16)
        self.span_ids = [int(span_id, 16) for span_id in span_ids]
        self.planning = True

    def detach(self) -> tuple[int | None, list[int], bool]:
        return self.trace_id, self.span_ids, self.planning

    def restore(self, previous: tuple[int | None, list[int], bool]) -> None:
        self.trace_id, self.span_ids, self.planning = previous

    def take_trace_id(self) -> int | None:
        if not self.planning:
            return None
        planned = self.trace_id
        self.trace_id = None
        return planned

    def take_span_id(self) -> int | None:
        if not self.planning or not self.span_ids:
            return None
        return self.span_ids.pop(0)


class CloudTraceExporter:
    # 业务 Cloud Trace 导出的唯一所有者：独立 provider、唯一客户端、有界队列与导出线程。
    def __init__(
        self,
        *,
        config: CloudTraceConfig,
        provider: TracerProvider,
        client: Langfuse,
        generator: _StableIdGenerator,
        tracer: Tracer,
    ) -> None:
        self._config = config
        self._provider = provider
        self._client = client
        self._generator = generator
        self._tracer = tracer
        self._queue: Queue[object] = Queue(maxsize=config.queue_size)
        self._lock = Lock()
        self._closed = False
        self._queue_full = 0
        self._close_timeout = 0
        self._thread = Thread(
            target=self._consume, name="fit-agent-cloud-trace", daemon=True
        )
        self._thread.start()

    @classmethod
    def create(
        cls,
        config: CloudTraceConfig,
        *,
        span_exporter: SpanExporter | None = None,
    ) -> "CloudTraceExporter | None":
        # 关闭状态不创建客户端、不校验可选凭据、不发起网络请求。
        if not config.enabled:
            return None
        problem = _config_problem(config)
        if problem is not None:
            logger.error("Cloud Trace 配置错误（%s）", problem)
            return None
        provider: TracerProvider | None = None
        client: Langfuse | None = None
        try:
            resource_attributes: dict[str, object] = {
                "service.name": "fit-agent",
                "langfuse.environment": config.environment,
            }
            if config.release is not None:
                resource_attributes["langfuse.release"] = config.release
            generator = _StableIdGenerator()
            provider = TracerProvider(
                resource=Resource.create(resource_attributes),
                id_generator=generator,
            )
            client = Langfuse(
                public_key=config.public_key,
                secret_key=config.secret_key,
                base_url=config.base_url,
                tracer_provider=provider,
                span_exporter=span_exporter,
                should_export_span=_should_export_span,
                environment=config.environment,
                release=config.release,
            )
            tracer = provider.get_tracer(FIT_AGENT_TRACER_NAME, FIT_AGENT_TRACER_VERSION)
            return cls(
                config=config,
                provider=provider,
                client=client,
                generator=generator,
                tracer=tracer,
            )
        except Exception as error:
            logger.error("Cloud Trace 初始化失败（%s）", type(error).__name__)
            if client is not None:
                with suppress(Exception):
                    client.shutdown()
            if provider is not None:
                with suppress(Exception):
                    provider.shutdown()
            return None

    @property
    def close_timeout_seconds(self) -> float:
        return self._config.close_timeout_seconds

    @property
    def dropped_cloud_queue_full(self) -> int:
        with self._lock:
            return self._queue_full

    @property
    def dropped_cloud_close_timeout(self) -> int:
        with self._lock:
            return self._close_timeout

    def submit(self, records: Sequence[SpanRecord]) -> None:
        # 同步非阻塞入队：热路径不等待 Cloud 网络；队满整条 Trace 丢弃。
        if not records:
            return
        self._enqueue(_SnapshotJob(tuple(records)))

    def submit_client_attempt(
        self,
        attempt: ClientAttemptRecord,
        *,
        trace_id: str,
        parent_span_id: int,
    ) -> None:
        if not _is_trace_id(trace_id):
            raise ValueError("trace_id 必须是 32 位非零小写 hex")
        if parent_span_id < 1:
            raise ValueError("parent_span_id 必须是正整数")
        self._enqueue(
            _ClientAttemptJob(
                attempt=attempt, trace_id=trace_id, parent_span_id=parent_span_id
            )
        )

    def close(self, timeout_seconds: float = CLOUD_TRACE_CLOSE_TIMEOUT_SECONDS) -> None:
        # 有界关闭：停止受理、排空队列并等待导出线程与 SDK 资源收尾。
        deadline = monotonic() + max(0.0, timeout_seconds)
        with self._lock:
            if self._closed:
                return
            self._closed = True
        queued = False
        try:
            self._queue.put(None, timeout=max(0.0, deadline - monotonic()))
            queued = True
        except Full:
            pass
        self._thread.join(max(0.0, deadline - monotonic()))
        if not queued or self._thread.is_alive():
            with self._lock:
                self._close_timeout += 1
            logger.error("Cloud Trace 关闭超时（dropped_cloud_close_timeout）")

    def _enqueue(self, job: object) -> None:
        with self._lock:
            if self._closed:
                return
        try:
            self._queue.put_nowait(job)
        except Full:
            with self._lock:
                self._queue_full += 1
                total = self._queue_full
            logger.warning("Cloud Trace 队列已满（dropped_cloud_queue_full/%s）", total)

    def _consume(self) -> None:
        # 导出线程只消费不可变快照，不访问业务对象与调用线程局部状态。
        while True:
            job = self._queue.get()
            if job is None:
                break
            try:
                self._export(job)
            except Exception as error:
                logger.error("Cloud Trace 导出失败（%s）", type(error).__name__)
        for release in (self._client.shutdown, self._provider.shutdown):
            try:
                release()
            except Exception as error:
                logger.error("Cloud Trace 资源释放失败（%s）", type(error).__name__)

    def _export(self, job: object) -> None:
        if isinstance(job, _SnapshotJob):
            self._export_snapshot(job.records)
        elif isinstance(job, _ClientAttemptJob):
            self._export_client_attempt(job)

    def _export_snapshot(self, records: Sequence[SpanRecord]) -> None:
        root = next((record for record in records if record.parent_span_id is None), None)
        if root is None:
            raise ValueError("快照缺少根 Span")
        identity = root.run_id if root.run_id is not None else root.operation_id
        trace_id = _cloud_trace_id(
            f"fit-agent/business/{'run' if root.run_id is not None else 'confirmation'}/{identity}"
        )
        planned = _plan_span_ids(identity, records)
        if root.span_id not in planned:
            raise ValueError("根 Span 无法保持稳定身份")
        ordered = [
            record for record in sorted(records, key=lambda item: item.span_id)
            if record.span_id in planned
        ]
        metadata = _trace_metadata(records, root)
        spans: dict[int, Span] = {}
        end_times: dict[int, int] = {}
        statuses: dict[int, Status | None] = {}
        with self._generator.plan(trace_id, [planned[record.span_id] for record in ordered]):
            for record in ordered:
                parent = (
                    None
                    if record.parent_span_id is None
                    else spans[record.parent_span_id]
                )
                start_ns, end_ns, anomaly = _record_times(record)
                attributes = _server_attributes(
                    record,
                    environment=self._config.environment,
                    release=self._config.release,
                    metadata=metadata,
                )
                attributes.update(anomaly)
                spans[record.span_id] = self._tracer.start_span(
                    record.name.value,
                    context=None if parent is None else trace.set_span_in_context(parent),
                    start_time=start_ns,
                    attributes=attributes,
                    record_exception=False,
                    set_status_on_exception=False,
                )
                end_times[record.span_id] = end_ns
                statuses[record.span_id] = _status_attributes(record)[1]
            for record in reversed(ordered):
                span = spans[record.span_id]
                if statuses[record.span_id] is not None:
                    span.set_status(statuses[record.span_id])
                span.end(end_time=end_times[record.span_id])

    def _export_client_attempt(self, job: _ClientAttemptJob) -> None:
        # 后置客户端节点：父 Context 显式指向业务根 Span，节点时间只表示服务器处理上报范围。
        attempt = job.attempt
        parent_id = _salted_hex(
            f"fit-agent/business/{attempt.run_id}/{job.parent_span_id}",
            set(),
            _SPAN_ID_LENGTH,
        )
        node_id = client_attempt_observation_id(
            attempt.run_id, attempt.client_attempt_id
        )
        if parent_id is None:
            raise ValueError("client_attempt 无法生成稳定 Span ID")
        start_ns = attempt.received_at_ms * 1_000_000
        end_ns = start_ns + round(attempt.processing_duration_ms * 1_000_000)
        # 已结算的根 Span 只以 SpanContext 参与关联；父上下文必须携带采样标记，
        # 否则 parentbased 采样器会丢弃后置客户端节点。
        parent = NonRecordingSpan(
            SpanContext(
                trace_id=int(job.trace_id, 16),
                span_id=int(parent_id, 16),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
        with self._generator.plan(job.trace_id, [node_id]):
            span = self._tracer.start_span(
                "client_attempt",
                context=trace.set_span_in_context(parent),
                start_time=start_ns,
                attributes=_attempt_attributes(attempt),
                record_exception=False,
                set_status_on_exception=False,
            )
            span.end(end_time=end_ns)
