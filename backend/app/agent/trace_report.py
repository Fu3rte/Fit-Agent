import argparse
import asyncio
import json
import statistics
import sys
from collections.abc import Sequence
from pathlib import Path
from time import time_ns
from uuid import UUID

import aiosqlite

from app.agent import telemetry
from app.agent.telemetry import SpanName, SpanRecord, SpanStatus

# 报告窗口与保留窗口一致：根 started_at 落在 [from, to) 的整条链路。
_RETENTION_MS = telemetry.RETENTION_DAYS * 86_400_000

# 分组顺序沿用契约列出的统计类型；turn 不进入统计组。
_GROUP_ORDER = {
    name: index
    for index, name in enumerate(("agent_run", "model", "tool", "compaction", "confirmation"))
}
_FIRST_TEXT_NAMES = (SpanName.agent_run, SpanName.model)
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_CACHE_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens")
_SLOW_LIMIT = 20

_COLUMNS = (
    "trace_id, span_id, parent_span_id, session_id, run_id, operation_id, name,"
    " started_at, finished_at, duration_ms, status, error_code, attributes"
)
_SELECT = f"SELECT {_COLUMNS} FROM spans"


def _readonly_uri(path: Path) -> str:
    # 只读 URI：数据库缺失或结构不兼容时直接打开失败，不创建、不迁移。
    return f"file:///{Path(path).as_posix().lstrip('/')}?mode=ro"


def _uuid_argument(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"非法 UUID: {value}") from error


def _resolve_window(from_ms: int | None, to_ms: int | None, now_ms: int) -> tuple[int, int]:
    lower = now_ms - _RETENTION_MS
    start = lower if from_ms is None else from_ms
    end = now_ms if to_ms is None else to_ms
    if not lower <= start <= now_ms:
        raise ValueError(f"--from 超出最近 {telemetry.RETENTION_DAYS} 天合法范围: {start}")
    if not lower <= end <= now_ms:
        raise ValueError(f"--to 超出最近 {telemetry.RETENTION_DAYS} 天合法范围: {end}")
    if start == end:
        raise ValueError("时间窗口为空区间")
    if start > end:
        raise ValueError("时间窗口为反向区间")
    return start, end


async def _fetch(sql: str, parameters: tuple) -> list[SpanRecord]:
    # 报告连接与采集写入相互独立：只读打开本地诊断库，始终释放 cursor 与 connection。
    connection = await aiosqlite.connect(
        _readonly_uri(telemetry.trace_database_path()), uri=True, isolation_level=None
    )
    connection.row_factory = aiosqlite.Row
    try:
        cursor = await connection.execute(sql, parameters)
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
    finally:
        await connection.close()
    return [
        SpanRecord.model_validate({**dict(row), "attributes": json.loads(row["attributes"])})
        for row in rows
    ]


def _distribution(values: Sequence[float]) -> dict:
    if not values:
        return {"samples": 0, "mean": None, "p50": None, "p95": None, "max": None}
    if len(values) == 1:
        value = float(values[0])
        return {"samples": 1, "mean": value, "p50": value, "p95": value, "max": value}
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "mean": statistics.fmean(ordered),
        "p50": statistics.median(ordered),
        "p95": statistics.quantiles(ordered, n=100, method="inclusive")[94],
        "max": max(ordered),
    }


def _usage(records: Sequence[SpanRecord]) -> dict:
    summary: dict = {}
    for field in _USAGE_FIELDS:
        values = [
            record.attributes[field]
            for record in records
            if record.attributes.get(field) is not None
        ]
        summary[field] = {"total": sum(values) if values else None, "samples": len(values)}
    common = [
        record
        for record in records
        if all(record.attributes.get(field) is not None for field in _CACHE_FIELDS)
    ]
    reads = sum(record.attributes["cache_read_tokens"] for record in common)
    total = sum(
        record.attributes["input_tokens"]
        + record.attributes["cache_read_tokens"]
        + record.attributes["cache_write_tokens"]
        for record in common
    )
    summary["cache_hit_rate"] = reads / total if total else None
    summary["cache_samples"] = len(common)
    return summary


def _slow_calls(records: Sequence[SpanRecord]) -> list[dict]:
    ordered = sorted(
        records,
        key=lambda record: (
            record.duration_ms,
            record.started_at,
            record.trace_id,
            record.span_id,
        ),
        reverse=True,
    )
    return [
        {
            "trace_id": record.trace_id,
            "span_id": record.span_id,
            "started_at": record.started_at,
            "duration_ms": record.duration_ms,
            "status": record.status.value,
        }
        for record in ordered[:_SLOW_LIMIT]
    ]


def _group_key(record: SpanRecord) -> tuple:
    name = record.name.value
    if record.name == SpanName.model:
        attributes = record.attributes
        return (
            name,
            attributes.get("provider"),
            attributes.get("model"),
            attributes.get("call_kind"),
            None,
        )
    if record.name == SpanName.tool:
        return (name, None, None, None, record.attributes.get("tool_name"))
    return (name, None, None, None, None)


def _group_sort_key(key: tuple) -> tuple:
    name, provider, model, call_kind, tool_name = key
    return (_GROUP_ORDER[name], provider or "", model or "", call_kind or "", tool_name or "")


def _summarize(key: tuple, records: Sequence[SpanRecord]) -> dict:
    name, provider, model, call_kind, tool_name = key
    calls = len(records)
    statuses = {status.value: 0 for status in SpanStatus}
    for record in records:
        statuses[record.status.value] += 1
    durations = [
        record.duration_ms for record in records if record.status == SpanStatus.completed
    ]
    group = {
        "key": {
            "name": name,
            "provider": provider,
            "model": model,
            "call_kind": call_kind,
            "tool_name": tool_name,
        },
        "calls": calls,
        "status_counts": statuses,
        "failure_rate": statuses["failed"] / calls,
        "cancellation_rate": statuses["cancelled"] / calls,
        "duration_ms": _distribution(durations),
        "first_text_ms": None,
        "usage": None,
        "slow_calls": _slow_calls(records),
    }
    if name in _FIRST_TEXT_NAMES:
        values = [
            record.attributes["first_text_ms"]
            for record in records
            if record.status == SpanStatus.completed
            and record.attributes.get("first_text_ms") is not None
        ]
        group["first_text_ms"] = _distribution(values)
    if name == SpanName.model:
        group["usage"] = _usage(records)
    return group


def _groups(records: Sequence[SpanRecord]) -> list[dict]:
    buckets: dict[tuple, list[SpanRecord]] = {}
    for record in records:
        if record.name == SpanName.turn:
            continue
        buckets.setdefault(_group_key(record), []).append(record)
    return [
        _summarize(key, buckets[key]) for key in sorted(buckets, key=_group_sort_key)
    ]


async def collect_metrics(
    *, from_ms: int | None, to_ms: int | None, session_id: str | None
) -> dict:
    now_ms = time_ns() // 1_000_000
    start, end = _resolve_window(from_ms, to_ms, now_ms)
    parameters: list = [start, end]
    session_clause = ""
    if session_id is not None:
        session_clause = " AND session_id = ?"
        parameters.append(session_id)
    sql = (
        f"{_SELECT} WHERE trace_id IN ("
        "SELECT trace_id FROM spans WHERE parent_span_id IS NULL"
        f" AND started_at >= ? AND started_at < ?{session_clause})"
        " ORDER BY trace_id, span_id"
    )
    records = await _fetch(sql, tuple(parameters))
    return {"window": {"from": start, "to": end}, "groups": _groups(records)}


async def collect_show(
    *, run_id: str | None, operation_id: str | None, session_id: str | None
) -> dict:
    now_ms = time_ns() // 1_000_000
    if run_id is not None:
        identity_clause = "name = 'agent_run' AND run_id = ?"
        identity = run_id
    else:
        identity_clause = "name = 'confirmation' AND operation_id = ?"
        identity = operation_id
    parameters: list = [identity, now_ms - _RETENTION_MS]
    session_clause = ""
    if session_id is not None:
        session_clause = " AND session_id = ?"
        parameters.append(session_id)
    sql = (
        f"{_SELECT} WHERE trace_id = ("
        f"SELECT trace_id FROM spans WHERE parent_span_id IS NULL AND {identity_clause}"
        f" AND started_at >= ?{session_clause})"
        " ORDER BY span_id"
    )
    records = await _fetch(sql, tuple(parameters))
    return {"spans": [record.model_dump(mode="json") for record in records]}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.agent.trace_report", description="本地 Trace JSON 报告"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    metrics = subparsers.add_parser("metrics", help="统计时间窗口内的分组指标")
    metrics.add_argument("--from", dest="from_ms", type=int, default=None, help="UTC 毫秒下界")
    metrics.add_argument("--to", dest="to_ms", type=int, default=None, help="UTC 毫秒上界")
    metrics.add_argument(
        "--session-id", dest="session_id", type=_uuid_argument, default=None
    )

    show = subparsers.add_parser("show", help="展示一次执行的完整链路")
    identity = show.add_mutually_exclusive_group(required=True)
    identity.add_argument("--run-id", dest="run_id", type=_uuid_argument, default=None)
    identity.add_argument(
        "--operation-id", dest="operation_id", type=_uuid_argument, default=None
    )
    show.add_argument("--session-id", dest="session_id", type=_uuid_argument, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "metrics":
            payload = asyncio.run(
                collect_metrics(
                    from_ms=args.from_ms, to_ms=args.to_ms, session_id=args.session_id
                )
            )
        else:
            payload = asyncio.run(
                collect_show(
                    run_id=args.run_id,
                    operation_id=args.operation_id,
                    session_id=args.session_id,
                )
            )
    except Exception as error:
        print(f"trace_report: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
