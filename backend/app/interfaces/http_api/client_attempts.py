import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    field_validator,
    model_validator,
)

from app.agent.cloud_trace import (
    ClientAttemptRecord,
    client_attempt_observation_id,
    cloud_trace_id_for_run,
)
from app.application.session.positions import now_ms
from app.domain.session.errors import (
    OperationExpired,
    RunNotFound,
    SessionMismatch,
    SessionNotFound,
)
from app.interfaces.http_api.contracts import PathUUID
from app.interfaces.http_api.errors import NO_STORE, reject

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_CLIENT_DURATION_MS = 86_400_000
RECEIPT_WINDOW_MS = 600_000
CACHE_CAPACITY = 4096
CACHE_TTL_SECONDS = 600.0
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

AttemptKey = tuple[str, str]


def attempt_uuid4(value: object) -> str:
    # 传输尝试身份固定 UUIDv4，标准带连字符形式；不接受其它版本或紧凑写法。
    if not isinstance(value, str):
        raise ValueError("身份必须为字符串")
    identifier = UUID(value)
    if identifier.version != 4 or str(identifier) != value.lower():
        raise ValueError("client_attempt_id 必须为 UUIDv4")
    return str(identifier)


def optional_measure(value: object) -> float | None:
    # 缺失保持 None；布尔值与非数值拒绝，数值必须有限。
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("耗时必须为数值")
    number = float(value)
    if not isfinite(number):
        raise ValueError("耗时必须为有限数值")
    return number


ClientAttemptID = Annotated[str, BeforeValidator(attempt_uuid4)]
ClientMeasure = Annotated[float | None, BeforeValidator(optional_measure)]


class ClientAttemptPayload(BaseModel):
    # 客户端诊断输入：仅批准的身份、枚举与测量；缺失测量以普通可选字段表达。
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    session_id: PathUUID
    operation_id: PathUUID
    run_id: PathUUID | None = None
    client_attempt_id: ClientAttemptID
    attempt_kind: Literal["send"]
    client_duration_ms: ClientMeasure = None
    client_first_text_ms: ClientMeasure = None
    client_terminal_kind: Literal[
        "completed", "failed", "cancelled", "disconnected", "unknown"
    ]

    @field_validator("client_duration_ms", "client_first_text_ms")
    @classmethod
    def measure_range(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= MAX_CLIENT_DURATION_MS:
            raise ValueError("耗时超出允许范围")
        return value

    @model_validator(mode="after")
    def first_text_within_duration(self):
        if (
            self.client_duration_ms is not None
            and self.client_first_text_ms is not None
            and self.client_first_text_ms > self.client_duration_ms
        ):
            raise ValueError("首正文不得晚于完成")
        return self


@dataclass(frozen=True)
class _Receipt:
    fingerprint: str
    expires_at: float


class ClientAttemptCache:
    # 进程内有界 TTL 缓存：同键同内容返回重复，同键不同内容返回冲突。
    def __init__(self, capacity: int, ttl_seconds: float) -> None:
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[AttemptKey, _Receipt] = OrderedDict()

    def classify(self, key: AttemptKey, fingerprint: str) -> str:
        self._prune()
        entry = self._entries.get(key)
        if entry is None:
            return "new"
        return "duplicate" if entry.fingerprint == fingerprint else "conflict"

    def put(self, key: AttemptKey, fingerprint: str) -> None:
        self._prune()
        self._entries.pop(key, None)
        self._entries[key] = _Receipt(
            fingerprint=fingerprint, expires_at=monotonic() + self._ttl_seconds
        )
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def _prune(self) -> None:
        deadline = monotonic()
        expired = [
            key for key, entry in self._entries.items()
            if entry.expires_at <= deadline
        ]
        for key in expired:
            del self._entries[key]


attempt_cache = ClientAttemptCache(CACHE_CAPACITY, CACHE_TTL_SECONDS)
_cache_lock = Lock()


def _fingerprint(payload: ClientAttemptPayload, run_id: str) -> str:
    # 服务端解析出的真实 run_id 统一表达 null 与真实值；接收时间与处理时长不进入指纹。
    normalized = payload.model_copy(update={"run_id": run_id}).model_dump()
    material = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _receipt(
    status_code: int,
    status: str,
    observation_id: str,
    *,
    reason: str | None = None,
) -> JSONResponse:
    body: dict[str, str] = {"status": status, "observation_id": observation_id}
    if reason is not None:
        body["reason"] = reason
    return JSONResponse(body, status_code, headers=NO_STORE)


@router.post("/api/agent/client-attempts")
async def record_client_attempt(payload: ClientAttemptPayload, request: Request):
    received_at_ms = now_ms()
    started_at = monotonic()
    app = request.app
    operations = app.state.session_operations
    try:
        outcome = await operations.get_operation_outcome(
            payload.session_id, payload.operation_id
        )
    except (OperationExpired, SessionNotFound, RunNotFound, SessionMismatch):
        outcome = None
    if (
        outcome is None
        or outcome.run is None
        or outcome.operation.kind != "send"
        or (payload.run_id is not None and payload.run_id != outcome.run.id)
    ):
        reject(409, "attempt_unassociated", "客户端观测未关联到已受理发送。")
    run = outcome.run
    if run.status == "running":
        reject(409, "run_not_terminal", "运行尚未取得终态。")
    if run.status not in _TERMINAL_STATUSES or run.finished_at is None:
        reject(410, "attempt_unreportable", "该运行无法关联客户端观测。")
    age_ms = received_at_ms - run.finished_at
    if not 0 <= age_ms <= RECEIPT_WINDOW_MS:
        reject(410, "attempt_expired", "客户端观测已超出接收窗口。")

    observation_id = client_attempt_observation_id(run.id, payload.client_attempt_id)
    fingerprint = _fingerprint(payload, run.id)
    key: AttemptKey = (run.id, payload.client_attempt_id)
    exporter = app.state.cloud_trace
    with _cache_lock:
        state = attempt_cache.classify(key, fingerprint)
        if state == "duplicate":
            return _receipt(200, "duplicate", observation_id)
        if state == "conflict":
            reject(409, "attempt_conflict", "同一传输尝试的内容不一致。")
        if exporter is None:
            attempt_cache.put(key, fingerprint)
            return _receipt(
                200, "skipped", observation_id, reason="cloud_unavailable"
            )
        attempt = ClientAttemptRecord(
            session_id=payload.session_id,
            operation_id=payload.operation_id,
            run_id=run.id,
            client_attempt_id=payload.client_attempt_id,
            attempt_kind=payload.attempt_kind,
            client_duration_ms=payload.client_duration_ms,
            client_first_text_ms=payload.client_first_text_ms,
            client_terminal_kind=payload.client_terminal_kind,
            received_at_ms=received_at_ms,
            processing_duration_ms=monotonic() - started_at,
        )
        try:
            exporter.submit_client_attempt(
                attempt,
                trace_id=cloud_trace_id_for_run(run.id),
                parent_span_id=1,
            )
        except Exception as error:
            # 观测提交异常只在观测边界隔离：不缓存本次结果，后续可用同一尝试重试。
            logger.error("客户端观测提交失败（%s）", type(error).__name__)
            reject(503, "observation_unavailable", "观测服务暂不可用。")
        attempt_cache.put(key, fingerprint)
    return _receipt(202, "recorded", observation_id)
