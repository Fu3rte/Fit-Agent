import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock
from time import perf_counter, time_ns
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self
from uuid import uuid4

if TYPE_CHECKING:
    from app.agent.cloud_trace import SpanExportSink

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 根 Span 只允许 agent_run 与 confirmation，其余名称只能作为子操作。
ROOT_SPAN_NAMES = ("agent_run", "confirmation")

# 观测保留窗口：按根 started_at 保留最近 30 天的整条 Trace。
RETENTION_DAYS = 30
_RETENTION_MS = RETENTION_DAYS * 86_400_000

SCHEMA_VERSION = 1

_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CONTROLLED_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_Measure = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_Token = Annotated[int, Field(ge=0)]
_Text = Annotated[str, Field(min_length=1)]
_TraceId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
_Uuid = Annotated[str, Field(pattern=_CANONICAL_UUID.pattern)]
_ErrorCode = Annotated[str, Field(pattern=_CONTROLLED_CODE.pattern)]


class SpanName(StrEnum):
    agent_run = "agent_run"
    turn = "turn"
    model = "model"
    tool = "tool"
    compaction = "compaction"
    confirmation = "confirmation"


class SpanStatus(StrEnum):
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class AgentRunAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_text_ms: _Measure | None = None


class TurnAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: _Text | None = None
    model: _Text | None = None
    call_kind: Literal["normal", "history_summary", "turn_prefix_summary"] | None = None
    stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"] | None = None
    first_text_ms: _Measure | None = None
    input_tokens: _Token | None = None
    output_tokens: _Token | None = None
    cache_read_tokens: _Token | None = None
    cache_write_tokens: _Token | None = None


class ToolAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: _Text | None = None
    tool_name: _Text | None = None
    execution_ms: _Measure | None = None
    postprocess_ms: _Measure | None = None


class CompactionAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["threshold", "overflow"] | None = None


class ConfirmationAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_kind: Literal["profile", "plan", "workout"] | None = None


_ATTRIBUTE_MODELS: dict[SpanName, type[BaseModel]] = {
    SpanName.agent_run: AgentRunAttributes,
    SpanName.turn: TurnAttributes,
    SpanName.model: ModelAttributes,
    SpanName.tool: ToolAttributes,
    SpanName.compaction: CompactionAttributes,
    SpanName.confirmation: ConfirmationAttributes,
}

# 开始时必填字段：缺失即拒绝建立该 Span；执行中只补充本次取得的字段。
_START_REQUIRED: dict[SpanName, frozenset[str]] = {
    SpanName.agent_run: frozenset(),
    SpanName.turn: frozenset(),
    SpanName.model: frozenset({"provider", "model", "call_kind"}),
    SpanName.tool: frozenset({"tool_call_id"}),
    SpanName.compaction: frozenset({"reason"}),
    SpanName.confirmation: frozenset({"business_kind"}),
}


def uuid4_hex() -> str:
    return uuid4().hex


def uuid4_canonical() -> str:
    return str(uuid4())


class SpanRecord(BaseModel):
    # 单条 Span 的数据契约：声明字段全部必填，未知值以 null 表达。
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: _TraceId
    span_id: int = Field(ge=1)
    parent_span_id: int | None = Field(ge=1)
    session_id: _Uuid
    run_id: _Uuid | None
    operation_id: _Uuid
    name: SpanName
    started_at: int = Field(ge=0)
    finished_at: int = Field(ge=0)
    duration_ms: _Measure
    status: SpanStatus
    error_code: _ErrorCode | None
    attributes: dict[str, Any]

    @field_validator("trace_id")
    @classmethod
    def _trace_is_uuidv4(cls, value: str) -> str:
        if int(value[12], 16) != 4:
            raise ValueError("trace_id 必须是 UUIDv4 的小写 hex")
        return value

    @model_validator(mode="after")
    def _check_identity(self) -> Self:
        _ATTRIBUTE_MODELS[self.name].model_validate(self.attributes)
        if self.parent_span_id == self.span_id:
            raise ValueError("Span 不能是自己的父节点")
        root = self.name.value in ROOT_SPAN_NAMES
        if root and self.parent_span_id is not None:
            raise ValueError(f"{self.name.value} 只能是根 Span")
        if self.parent_span_id is None and not root:
            raise ValueError(f"{self.name.value} 必须挂在父 Span 下")
        if self.parent_span_id is None and (self.name is SpanName.agent_run) != (
            self.run_id is not None
        ):
            raise ValueError("Agent 根记录携带 run_id，确认根记录 run_id 为 null")
        return self

    def to_row(self) -> tuple:
        return (
            self.trace_id,
            self.span_id,
            self.parent_span_id,
            self.session_id,
            self.run_id,
            self.operation_id,
            self.name.value,
            self.started_at,
            self.finished_at,
            self.duration_ms,
            self.status.value,
            self.error_code,
            json.dumps(
                self.attributes, ensure_ascii=False, allow_nan=False, sort_keys=True
            ),
        )


def trace_database_path() -> Path:
    # 观测库固定为 backend/data/trace.db，与业务库和缓存观测目录分离。
    return Path(__file__).resolve().parents[2] / "data" / "trace.db"


def _checked_start_attributes(
    name: SpanName, attributes: Mapping[str, Any] | None
) -> dict[str, Any]:
    payload = dict(attributes or {})
    missing = _START_REQUIRED[name] - {key for key, value in payload.items() if value is not None}
    if missing:
        raise ValueError(f"缺少必填属性: {sorted(missing)}")
    return _ATTRIBUTE_MODELS[name].model_validate(payload).model_dump(exclude_none=True)


def _automatic_status(error: BaseException | None) -> SpanStatus:
    if error is None:
        return SpanStatus.completed
    # 任务取消与协作取消均按真实控制流结算为 cancelled。
    if isinstance(error, (asyncio.CancelledError, FutureCancelledError)):
        return SpanStatus.cancelled
    return SpanStatus.failed


@dataclass
class _SpanState:
    span_id: int
    parent_span_id: int | None
    name: SpanName
    attributes: dict[str, Any]
    status: SpanStatus
    error_code: str | None
    explicit_status: bool
    started_at: int
    started_perf: float
    finished_at: int | None = None
    duration_ms: float | None = None
    settled: bool = False


class TraceSpan:
    # 生命周期托管句柄，本身即显式父上下文；记录方法被动、同步、不抛出。
    # state 为空表示无记录上下文：callback 照常执行，不产生任何记录。
    __slots__ = ("_trace", "_state")

    def __init__(self, trace: "RunTrace | None" = None, state: _SpanState | None = None) -> None:
        self._trace = trace
        self._state = state

    def start_span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None,
        callback: Callable[["TraceSpan"], Any],
    ) -> Any:
        state = self._state
        if state is None:
            # 无记录上下文：子操作沿用同一句柄，callback 仍恰好执行一次。
            return callback(self)
        return self._trace._begin(state, name, attributes, callback)

    def open(self, name: str, attributes: Mapping[str, Any] | None = None) -> "TraceSpan":
        # 跨阶段生命周期的子 Span：登记与结算沿用同一记录器逻辑，调用方显式 settle。
        # 登记失败时返回无记录句柄，调用方后续记录与结算均为空操作。
        state = self._state
        if state is None or self._trace is None:
            return TraceSpan(self._trace)
        created: _SpanState | None = None
        with suppress(Exception):
            created = self._trace._create(state, name, attributes)
        return TraceSpan(self._trace, created)

    def settle(self, error: BaseException | None = None) -> None:
        # 仅供 open 登记的跨阶段 Span 显式结算；结算逻辑与 callback 路径一致且幂等。
        state = self._state
        if state is not None:
            self._trace._settle(state, error)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        state = self._state
        if state is not None:
            with suppress(Exception):
                self._trace._merge_attributes(state, attributes)

    def set_status(self, status: str, error_code: str | None = None) -> None:
        state = self._state
        if state is not None:
            with suppress(Exception):
                self._trace._record_status(state, status, error_code)

    @property
    def trace_id(self) -> str:
        # 本 Span 所属 Trace 的观测身份：入口据此统一内部事件身份与记录身份。
        return self._trace.trace_id if self._trace is not None else ""

    def record_first_text(self) -> None:
        # 首个可见正文进入公开队列时调用：按本 Span 起点计时，只写入一次。
        state = self._state
        if state is not None:
            with suppress(Exception):
                self._trace._record_first_text(state)


class RunTrace:
    # 一次运行的内存记录器：Trace 身份与业务身份由根统一传给全部子 Span。
    def __init__(
        self,
        *,
        session_id: str,
        operation_id: str,
        run_id: str | None = None,
        trace_id: str | None = None,
        contains: Callable[[object], bool] | None = None,
        accepted_at: int | None = None,
        accepted_perf: float | None = None,
    ) -> None:
        self.trace_id = uuid4_hex() if trace_id is None else trace_id
        self.session_id = session_id
        self.run_id = run_id
        self.operation_id = operation_id
        self._contains = contains
        self._accepted_at = accepted_at
        self._accepted_perf = accepted_perf
        self._lock = Lock()
        self._spans: list[_SpanState] = []
        self._root: _SpanState | None = None
        self._next_span_id = 1
        self._released = False

    @property
    def root_span_id(self) -> int | None:
        with self._lock:
            return None if self._root is None else self._root.span_id

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def start_span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None,
        callback: Callable[[TraceSpan], Any],
    ) -> Any:
        return self._begin(None, name, attributes, callback)

    def _begin(
        self,
        parent: _SpanState | None,
        name: str,
        attributes: Mapping[str, Any] | None,
        callback: Callable[[TraceSpan], Any],
    ) -> Any:
        state: _SpanState | None = None
        with suppress(Exception):
            state = self._create(parent, name, attributes)
        if state is None:
            return callback(TraceSpan(self))
        try:
            result = callback(TraceSpan(self, state))
        except BaseException as error:
            self._settle(state, error)
            raise
        if isinstance(result, Awaitable):
            return _await_and_settle(self, state, result)
        self._settle(state, None)
        return result

    def _create(
        self, parent: _SpanState | None, name: str, attributes: Mapping[str, Any] | None
    ) -> _SpanState:
        with self._lock:
            if self._released:
                raise RuntimeError("记录器已释放")
            span_name = SpanName(name)
            if parent is not None and parent.settled:
                raise RuntimeError("父 Span 已结算")
            if parent is None:
                if self._root is not None or span_name.value not in ROOT_SPAN_NAMES:
                    raise RuntimeError("根 Span 身份不符合数据契约")
            root = parent is None
            state = _SpanState(
                span_id=self._next_span_id,
                parent_span_id=None if root else parent.span_id,
                name=span_name,
                attributes=_checked_start_attributes(span_name, attributes),
                status=SpanStatus.completed,
                error_code=None,
                explicit_status=False,
                started_at=(
                    self._accepted_at
                    if root and self._accepted_at is not None
                    else time_ns() // 1_000_000
                ),
                started_perf=(
                    self._accepted_perf
                    if root and self._accepted_perf is not None
                    else perf_counter()
                ),
            )
            self._next_span_id += 1
            self._spans.append(state)
            if root:
                self._root = state
            return state

    def _merge_attributes(
        self, state: _SpanState, attributes: Mapping[str, Any], *, once: str | None = None
    ) -> None:
        with self._lock:
            if self._released or state.settled or (once and once in state.attributes):
                return
            model = _ATTRIBUTE_MODELS[state.name]
            merged = dict(state.attributes)
            merged.update(model.model_validate(dict(attributes)).model_dump(exclude_none=True))
            state.attributes = merged

    def _record_status(self, state: _SpanState, status: str, error_code: str | None) -> None:
        with self._lock:
            if self._released or state.settled:
                return
            value = SpanStatus(status)
            if error_code is not None and not _CONTROLLED_CODE.fullmatch(error_code):
                raise ValueError("error_code 必须是受控错误码")
            state.status = value
            state.error_code = error_code
            state.explicit_status = True

    def _record_first_text(self, state: _SpanState) -> None:
        # 首正文延迟按被观测 Span 的起点实测：根起点即受理 perf_counter；重复调用保持首次取值。
        elapsed = max(0.0, (perf_counter() - state.started_perf) * 1000)
        self._merge_attributes(state, {"first_text_ms": elapsed}, once="first_text_ms")

    def _settle(self, state: _SpanState, error: BaseException | None) -> None:
        # 结算属纯观测操作：故障时记录保持未结算，在快照阶段连同子树被拒绝；
        # 业务返回值与原始业务异常不受影响。
        try:
            with self._lock:
                if self._released or state.settled:
                    return
                finished_at = time_ns() // 1_000_000
                duration_ms = max(0.0, (perf_counter() - state.started_perf) * 1000)
                status = state.status
                if not state.explicit_status:
                    status = _automatic_status(error)
                state.status = status
                state.finished_at = finished_at
                state.duration_ms = duration_ms
                state.settled = True
        except Exception:
            pass

    def records(self) -> list[SpanRecord]:
        # 快照按 span_id 升序，父记录先于子记录；被拒绝的记录连同其子树一并剔除。
        with self._lock:
            states = sorted(self._spans, key=lambda item: item.span_id)
        kept: list[SpanRecord] = []
        rejected: set[int] = set()
        for state in states:
            if state.parent_span_id in rejected:
                rejected.add(state.span_id)
                continue
            try:
                record = SpanRecord(
                    trace_id=self.trace_id,
                    span_id=state.span_id,
                    parent_span_id=state.parent_span_id,
                    session_id=self.session_id,
                    run_id=self.run_id,
                    operation_id=self.operation_id,
                    name=state.name,
                    started_at=state.started_at,
                    finished_at=state.finished_at,
                    duration_ms=state.duration_ms,
                    status=state.status,
                    error_code=state.error_code,
                    attributes=dict(state.attributes),
                )
            except Exception:
                rejected.add(state.span_id)
                continue
            if self._contains is not None and self._contains(record.model_dump()):
                rejected.add(state.span_id)
                continue
            kept.append(record)
        return kept

    def release(self) -> None:
        with self._lock:
            self._spans = []
            self._released = True

    async def finish(
        self,
        store: "TraceStore | None",
        sink: "SpanExportSink | None" = None,
    ) -> None:
        # 观测收尾边界：快照只生成一次，本地写入与 Cloud 入队分别隔离，允许整条缺样。
        # 取消与进程退出沿用原有控制流；两条路径都释放记录器。
        try:
            records = self.records()
            if records:
                if store is not None:
                    try:
                        await store.save(records)
                    except Exception:
                        pass
                if sink is not None:
                    try:
                        sink.submit(records)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self.release()


async def _await_and_settle(
    trace: RunTrace, state: _SpanState, awaitable: Awaitable[Any]
) -> Any:
    try:
        result = await awaitable
    except BaseException as error:
        trace._settle(state, error)
        raise
    trace._settle(state, None)
    return result


_SCHEMA_SQL = """
CREATE TABLE spans (
    trace_id TEXT NOT NULL,
    span_id INTEGER NOT NULL CHECK (span_id > 0),
    parent_span_id INTEGER,
    session_id TEXT NOT NULL,
    run_id TEXT,
    operation_id TEXT NOT NULL,
    name TEXT NOT NULL CHECK (name IN
        ('agent_run','turn','model','tool','compaction','confirmation')),
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    duration_ms REAL NOT NULL CHECK (duration_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('completed','failed','cancelled')),
    error_code TEXT,
    attributes TEXT NOT NULL CHECK (json_valid(attributes)),
    PRIMARY KEY (trace_id, span_id),
    FOREIGN KEY (trace_id, parent_span_id) REFERENCES spans (trace_id, span_id)
        ON DELETE CASCADE
) STRICT;
CREATE UNIQUE INDEX spans_agent_root ON spans (run_id) WHERE name = 'agent_run';
CREATE UNIQUE INDEX spans_confirmation_root ON spans (operation_id) WHERE name = 'confirmation';
"""

_COLUMNS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "session_id",
    "run_id",
    "operation_id",
    "name",
    "started_at",
    "finished_at",
    "duration_ms",
    "status",
    "error_code",
    "attributes",
)
_INDEXES = frozenset({"spans_agent_root", "spans_confirmation_root"})
_INSERT_SQL = (
    "INSERT INTO spans (trace_id, span_id, parent_span_id, session_id, run_id,"
    " operation_id, name, started_at, finished_at, duration_ms, status, error_code,"
    " attributes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT_SQL = (
    "SELECT trace_id, span_id, parent_span_id, session_id, run_id, operation_id,"
    " name, started_at, finished_at, duration_ms, status, error_code, attributes"
    " FROM spans"
)


class TraceStore:
    # 观测库单表存储：整条 Trace 用一个事务尝试提交，过期链路在同事务内清理。
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def save(self, records: Sequence[SpanRecord], *, now_ms: int | None = None) -> None:
        await self._connection.execute("BEGIN")
        try:
            await self.purge_expired(now_ms=now_ms)
            await self._connection.executemany(_INSERT_SQL, [record.to_row() for record in records])
            await self._connection.execute("COMMIT")
        except BaseException:
            # 提交锁冲突同样在本范围内收尾：事务结束后连接才能承接后续 Trace。
            if self._connection.in_transaction:
                await self._connection.execute("ROLLBACK")
            raise

    async def purge_expired(self, *, now_ms: int | None = None) -> int:
        # 只按根记录的 started_at 裁剪整条链路，业务库不在本库范围内。
        cutoff = (time_ns() // 1_000_000 if now_ms is None else now_ms) - _RETENTION_MS
        cursor = await self._connection.execute(
            "DELETE FROM spans WHERE parent_span_id IS NULL AND started_at < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        await cursor.close()
        return deleted

    async def read_trace(self, trace_id: str) -> list[SpanRecord]:
        rows = await self._rows(f"{_SELECT_SQL} WHERE trace_id = ? ORDER BY span_id", (trace_id,))
        return [
            SpanRecord.model_validate({**row, "attributes": json.loads(row["attributes"])})
            for row in rows
        ]

    async def trace_count(self) -> int:
        cursor = await self._connection.execute("SELECT count(*) FROM spans")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0])

    async def close(self) -> None:
        await self._connection.close()

    async def _rows(self, sql: str, parameters: tuple) -> list[dict]:
        cursor = await self._connection.execute(sql, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def open_trace_store(path: Path | str | None = None) -> TraceStore:
    resolved = Path(path) if path is not None else trace_database_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(resolved, isolation_level=None)
    connection.row_factory = aiosqlite.Row
    try:
        await connection.execute("PRAGMA foreign_keys = ON")
        cursor = await connection.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row[0] != 1:
            raise RuntimeError("无法开启外键约束 PRAGMA foreign_keys = ON")
        cursor = await connection.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        version = 0 if row is None else int(row[0])
        if version == SCHEMA_VERSION:
            await _verify_schema(connection)
        elif version == 0:
            existing = await _application_objects(connection)
            if existing:
                raise RuntimeError(f"观测库 user_version 为 0 但已存在结构：{existing}")
            await connection.executescript(
                f"BEGIN;{_SCHEMA_SQL}PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
            )
            await _verify_schema(connection)
        else:
            raise RuntimeError(f"不支持的观测库 schema 版本：{version}")
        store = TraceStore(connection)
        # 初始化即按根开始时间裁剪过期链路：清理失败时关闭本次新建的连接后传播原错误。
        await store.purge_expired()
        return store
    except BaseException:
        await connection.close()
        raise


async def connect_trace_store(path: Path | str | None = None) -> TraceStore | None:
    # 开库属纯观测操作：故障时返回 None，本次运行不落库，业务路径保持不变。
    with suppress(Exception):
        return await open_trace_store(path)
    return None


async def _verify_schema(connection: aiosqlite.Connection) -> None:
    cursor = await connection.execute("PRAGMA table_info(spans)")
    columns = [row[1] for row in await cursor.fetchall()]
    await cursor.close()
    if columns != list(_COLUMNS):
        raise RuntimeError(f"观测库 spans 列不兼容：{columns}")
    names = await _application_objects(connection)
    if not _INDEXES <= set(names):
        raise RuntimeError(f"观测库结构缺失：{sorted(_INDEXES - set(names))}")


async def _application_objects(connection: aiosqlite.Connection) -> list[str]:
    cursor = await connection.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [row[0] for row in rows]
