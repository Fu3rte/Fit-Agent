# 运行：在 backend 执行 python -m test.check_trace_report
# 本地 Trace JSON 报告 CLI：真实 SpanRecord + TraceStore + open_trace_store 写隔离 SQLite，
# 真实 main(argv) 走 argparse 与 JSON 输出；统计断言全部用独立已知值，不调用被测计算函数。
import asyncio
import contextlib
import io
import json
import shutil
import sqlite3
from pathlib import Path
from time import time_ns

import pytest

from app.agent import telemetry, trace_report
from app.agent.telemetry import SpanRecord, open_trace_store, uuid4_canonical, uuid4_hex
from test.regression_support import patch_trace_database

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND / "temp" / "trace-report"
DBS = ROOT / "dbs"
EVIDENCE = ROOT / "evidence.json"
DAY_MS = 86_400_000
RETENTION_MS = telemetry.RETENTION_DAYS * DAY_MS
NOW = time_ns() // 1_000_000

RESULTS: dict[str, object] = {}

_INSERT = (
    "INSERT INTO spans (trace_id, span_id, parent_span_id, session_id, run_id,"
    " operation_id, name, started_at, finished_at, duration_ms, status, error_code,"
    " attributes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


# ---------- 隔离与运行助手 ----------

def run(coroutine):
    return asyncio.run(coroutine)


def database(name: str) -> Path:
    DBS.mkdir(parents=True, exist_ok=True)
    path = DBS / f"{name}.db"
    path.unlink(missing_ok=True)
    return path


def write_database(name: str, records, *, now_ms: int | None = None) -> Path:
    path = database(name)

    async def scenario() -> None:
        store = await open_trace_store(path)
        try:
            await store.save(records, now_ms=now_ms)
        finally:
            await store.close()

    run(scenario())
    return path


def empty_database(name: str) -> Path:
    return write_database(name, [])


@contextlib.contextmanager
def use_database(path):
    # 报告实现的 _fetch 通过 telemetry.trace_database_path() 模块属性查找取库：
    # 同进程内改写该属性即可把真实 main 指向隔离库，无需任何生产开关。
    original = telemetry.trace_database_path
    telemetry.trace_database_path = lambda: Path(path)
    try:
        yield
    finally:
        telemetry.trace_database_path = original


def run_main(argv) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = trace_report.main(list(argv))
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 0
    return code, out.getvalue(), err.getvalue()


def success(path, argv) -> dict:
    with use_database(path):
        code, out, err = run_main(argv)
    assert code == 0, (argv, code, out, err)
    return json.loads(out)


def failure(path, argv) -> str:
    # 失败路径：非零退出（argparse 与数据错误都为 2），stdout 不含 JSON。
    with use_database(path):
        code, out, err = run_main(argv)
    assert code == 2, (argv, code, out, err)
    assert out == "", (argv, out)
    return err


# ---------- 样本构造助手（参考 check_trace_storage.py）----------

def identity(**overrides) -> dict:
    values = {
        "trace_id": uuid4_hex(),
        "session_id": uuid4_canonical(),
        "run_id": uuid4_canonical(),
        "operation_id": uuid4_canonical(),
    }
    values.update(overrides)
    return values


def record(
    span_id: int,
    parent: int | None,
    name: str,
    *,
    started_at: int,
    attributes: dict | None = None,
    duration: float = 5.0,
    status: str = "completed",
    error_code: str | None = None,
    **fields,
) -> SpanRecord:
    payload = {
        **identity(),
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "started_at": started_at,
        "finished_at": started_at + 5,
        "duration_ms": duration,
        "status": status,
        "error_code": error_code,
        "attributes": {} if attributes is None else attributes,
    }
    payload.update(fields)
    return SpanRecord.model_validate(payload)


def trace_hex(value: int) -> str:
    # UUIDv4 形状的 32 位小写 hex（第 13 位为 4），可用于确定性排序断言。
    return "deadbeef0000" + "4" + f"{value:019x}"


def groups_by_name(payload: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for group in payload["groups"]:
        grouped.setdefault(group["key"]["name"], []).append(group)
    return grouped


def assert_distribution(actual, *, samples, mean, p50, p95, maximum) -> None:
    assert actual["samples"] == samples
    for key, expected in (("mean", mean), ("p50", p50), ("p95", p95), ("max", maximum)):
        if expected is None:
            assert actual[key] is None
        else:
            assert actual[key] == pytest.approx(expected)


# ---------- 1. 空库与合法无匹配 ----------

def test_empty_and_no_match() -> None:
    empty = empty_database("empty")
    payload = success(empty, ["metrics"])
    assert set(payload) == {"window", "groups"}
    assert payload["groups"] == []
    assert isinstance(payload["window"]["from"], int)
    assert isinstance(payload["window"]["to"], int)
    assert payload["window"]["to"] - payload["window"]["from"] == RETENTION_MS
    narrow = success(empty, ["metrics", "--from", str(NOW - 2000), "--to", str(NOW - 1000)])
    assert narrow["window"] == {"from": NOW - 2000, "to": NOW - 1000}
    assert narrow["groups"] == []
    assert success(empty, ["show", "--run-id", uuid4_canonical()]) == {"spans": []}
    assert success(empty, ["show", "--operation-id", uuid4_canonical()]) == {"spans": []}

    # 合法无匹配：库内确有记录，只是窗口未覆盖任何根。
    rooted = write_database(
        "nomatch",
        [record(1, None, "agent_run", started_at=NOW - 5000, duration=10.0)],
    )
    payload = success(rooted, ["metrics", "--from", str(NOW - 2000), "--to", str(NOW - 1000)])
    assert payload["groups"] == []
    assert success(rooted, ["show", "--run-id", uuid4_canonical()]) == {"spans": []}
    RESULTS["empty_no_match"] = {
        "empty_groups": [],
        "empty_window_span_ms": RETENTION_MS,
        "no_match_groups": payload["groups"],
    }


# ---------- 2. 根时间筛选 ----------

def test_metrics_root_window_selection() -> None:
    wfrom = NOW - 20_000
    wto = NOW - 10_000
    session = uuid4_canonical()
    other = uuid4_canonical()
    records = []
    trace_a = identity(trace_id=uuid4_hex(), session_id=uuid4_canonical(), run_id=uuid4_canonical())
    records += [
        record(1, None, "agent_run", started_at=wfrom, duration=10.0, **trace_a),
        record(
            2,
            1,
            "model",
            started_at=wfrom - 5000,
            duration=20.0,
            attributes={"provider": "P", "model": "M", "call_kind": "normal"},
            **trace_a,
        ),
    ]
    records.append(record(1, None, "agent_run", started_at=wto, duration=10.0, **identity()))
    records.append(record(1, None, "agent_run", started_at=wto - 1, duration=10.0, **identity()))
    records.append(record(1, None, "agent_run", started_at=wfrom - 1, duration=10.0, **identity()))
    records.append(
        record(1, None, "agent_run", started_at=wfrom + 50, duration=10.0, **identity(session_id=session))
    )
    records.append(
        record(1, None, "agent_run", started_at=wfrom + 50, duration=10.0, **identity(session_id=other))
    )
    path = write_database("window", records)

    payload = success(path, ["metrics", "--from", str(wfrom), "--to", str(wto)])
    assert payload["window"] == {"from": wfrom, "to": wto}
    # from 包含、to 排除：仅 A/C/S/S2 四个根命中；A 的 model 子操作时间在窗口外仍随链路保留。
    counts = {g["key"]["name"]: g["calls"] for g in payload["groups"]}
    assert counts == {"agent_run": 4, "model": 1}
    assert [g["key"]["name"] for g in payload["groups"]] == ["agent_run", "model"]

    scoped = success(path, ["metrics", "--from", str(wfrom), "--to", str(wto), "--session-id", session])
    assert {g["key"]["name"]: g["calls"] for g in scoped["groups"]} == {"agent_run": 1}

    only_from = success(path, ["metrics", "--from", str(wfrom)])
    assert groups_by_name(only_from)["agent_run"][0]["calls"] == 5
    only_to = success(path, ["metrics", "--to", str(wto)])
    assert groups_by_name(only_to)["agent_run"][0]["calls"] == 5
    default = success(path, ["metrics"])
    assert groups_by_name(default)["agent_run"][0]["calls"] == 6
    assert default["window"]["to"] - default["window"]["from"] == RETENTION_MS
    assert groups_by_name(success(path, ["metrics", "--session-id", session]))["agent_run"][0]["calls"] == 1
    RESULTS["window"] = {
        "in_window_calls": counts,
        "only_from": 5,
        "only_to": 5,
        "default": 6,
        "session_scoped": 1,
    }


# ---------- 3. show 身份、顺序、会话、过期 ----------

def test_show_identity_order_session_expiry() -> None:
    run_id = uuid4_canonical()
    session = uuid4_canonical()
    other = uuid4_canonical()
    operation_id = uuid4_canonical()
    confirmation_session = uuid4_canonical()
    expired_run = uuid4_canonical()
    expired_session = uuid4_canonical()

    agent = identity(trace_id=uuid4_hex(), session_id=session, run_id=run_id)
    records = [
        record(1, None, "agent_run", started_at=NOW - 1000, attributes={"first_text_ms": 1.0}, **agent),
        record(2, 1, "turn", started_at=NOW - 990, **agent),
        record(
            3,
            2,
            "model",
            started_at=NOW - 980,
            attributes={"provider": "p", "model": "m", "call_kind": "normal"},
            **agent,
        ),
        record(4, 2, "tool", started_at=NOW - 970, attributes={"tool_call_id": "c1", "tool_name": "echo"}, **agent),
    ]
    confirmation = identity(
        trace_id=uuid4_hex(), session_id=confirmation_session, run_id=None, operation_id=operation_id
    )
    records += [
        record(1, None, "confirmation", started_at=NOW - 500, attributes={"business_kind": "plan"}, **confirmation),
        record(2, 1, "compaction", started_at=NOW - 490, attributes={"reason": "threshold"}, **confirmation),
    ]
    expired_start = NOW - 31 * DAY_MS
    expired = identity(trace_id=uuid4_hex(), session_id=expired_session, run_id=expired_run)
    expired_records = [
        record(1, None, "agent_run", started_at=expired_start, **expired),
        record(2, 1, "turn", started_at=expired_start + 1, **expired),
    ]

    path = database("show")

    async def scenario() -> None:
        store = await open_trace_store(path)
        try:
            await store.save(records, now_ms=NOW)
            # 过期链路用更早的写入时刻清理点，避免写入事务内的清理先于插入把它删掉。
            await store.save(expired_records, now_ms=expired_start + 2 * DAY_MS)
        finally:
            await store.close()

    run(scenario())

    shown = success(path, ["show", "--run-id", run_id])
    assert [span["span_id"] for span in shown["spans"]] == [1, 2, 3, 4]
    assert [span["name"] for span in shown["spans"]] == ["agent_run", "turn", "model", "tool"]
    assert set(shown["spans"][0]) >= {
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
    }
    assert shown["spans"][0]["run_id"] == run_id
    assert shown["spans"][0]["attributes"] == {"first_text_ms": 1.0}

    assert [span["span_id"] for span in success(path, ["show", "--run-id", run_id, "--session-id", session])["spans"]] == [
        1,
        2,
        3,
        4,
    ]
    # 会话不符：同一 run-id 在别的会话下返回空。
    assert success(path, ["show", "--run-id", run_id, "--session-id", other]) == {"spans": []}
    confirmed = success(path, ["show", "--operation-id", operation_id])
    assert [span["span_id"] for span in confirmed["spans"]] == [1, 2]
    assert [span["name"] for span in confirmed["spans"]] == ["confirmation", "compaction"]

    # 过期根：记录确在库内，但限定 started_at >= now-30d 使其被排除。
    connection = sqlite3.connect(path)
    try:
        stored = connection.execute(
            "SELECT count(*) FROM spans WHERE trace_id = ?", (expired["trace_id"],)
        ).fetchone()[0]
    finally:
        connection.close()
    assert stored == 2
    assert success(path, ["show", "--run-id", expired_run]) == {"spans": []}
    assert success(path, ["show", "--run-id", uuid4_canonical()]) == {"spans": []}
    RESULTS["show"] = {
        "agent_spans": [1, 2, 3, 4],
        "confirmation_spans": [1, 2],
        "session_mismatch": [],
        "expired_rows_in_db": stored,
        "expired_result": [],
    }


# ---------- 4. 参数与窗口错误 ----------

def test_parameter_failures() -> None:
    path = write_database(
        "params",
        [record(1, None, "agent_run", started_at=NOW - 1000)],
    )
    run_id = uuid4_canonical()
    cases = {
        "无子命令": [],
        "show 缺身份": ["show"],
        "show 双身份": ["show", "--run-id", run_id, "--operation-id", uuid4_canonical()],
        "非整数毫秒": ["metrics", "--from", "not-int"],
        "浮点毫秒": ["metrics", "--from", "1.5"],
        "非法 run-id UUID": ["show", "--run-id", "not-a-uuid"],
        "非法 session UUID": ["metrics", "--session-id", "zzz"],
    }
    data_cases = {
        "from 越界": ["metrics", "--from", str(NOW + 10 * DAY_MS)],
        "to 越界": ["metrics", "--to", str(NOW - 40 * DAY_MS)],
        "空区间": ["metrics", "--from", str(NOW - 1000), "--to", str(NOW - 1000)],
        "反向区间": ["metrics", "--from", str(NOW - 1000), "--to", str(NOW - 2000)],
    }
    observed = {}
    for label, argv in cases.items():
        err = failure(path, argv)
        observed[label] = err.strip().splitlines()[-1] if err.strip() else ""
        assert err.strip() != ""
    for label, argv in data_cases.items():
        err = failure(path, argv)
        observed[label] = err.strip()
        assert err.startswith("trace_report: ")
        assert "ValueError" in err
    RESULTS["parameters"] = observed


# ---------- 5. 分组完整性 ----------

def test_group_completeness() -> None:
    shared = identity(trace_id=uuid4_hex(), run_id=uuid4_canonical())
    records = [
        record(1, None, "agent_run", started_at=NOW - 1000, attributes={"first_text_ms": 1.0}, **shared),
        record(2, 1, "turn", started_at=NOW - 990, **shared),
        record(
            3,
            2,
            "model",
            started_at=NOW - 980,
            attributes={"provider": "p1", "model": "m1", "call_kind": "normal"},
            **shared,
        ),
        record(
            4,
            2,
            "model",
            started_at=NOW - 970,
            attributes={"provider": "p2", "model": "m2", "call_kind": "normal"},
            **shared,
        ),
        record(
            5,
            2,
            "model",
            started_at=NOW - 960,
            attributes={"provider": "p1", "model": "m1", "call_kind": "history_summary"},
            **shared,
        ),
        record(6, 2, "tool", started_at=NOW - 950, attributes={"tool_call_id": "c1", "tool_name": "echo"}, **shared),
        record(7, 2, "tool", started_at=NOW - 940, attributes={"tool_call_id": "c2"}, **shared),
        record(8, 2, "compaction", started_at=NOW - 930, attributes={"reason": "threshold"}, **shared),
    ]
    confirmation = identity(trace_id=uuid4_hex(), run_id=None)
    records.append(
        record(1, None, "confirmation", started_at=NOW - 900, attributes={"business_kind": "plan"}, **confirmation)
    )
    path = write_database("groups", records)
    payload = success(path, ["metrics"])
    names = [g["key"]["name"] for g in payload["groups"]]
    assert names == ["agent_run", "model", "model", "model", "tool", "tool", "compaction", "confirmation"]
    assert "turn" not in names

    by_name = groups_by_name(payload)
    assert [tuple(g["key"][field] for field in ("provider", "model", "call_kind")) for g in by_name["model"]] == [
        ("p1", "m1", "history_summary"),
        ("p1", "m1", "normal"),
        ("p2", "m2", "normal"),
    ]
    assert [g["key"]["tool_name"] for g in by_name["tool"]] == [None, "echo"]
    assert by_name["agent_run"][0]["key"] == {
        "name": "agent_run",
        "provider": None,
        "model": None,
        "call_kind": None,
        "tool_name": None,
    }
    # usage 仅 model 组非空；first_text 仅 agent_run 与 model 组为统计对象。
    assert by_name["model"][0]["usage"] is not None
    for name in ("agent_run", "tool", "compaction", "confirmation"):
        assert by_name[name][0]["usage"] is None
    assert by_name["agent_run"][0]["first_text_ms"] is not None
    assert by_name["model"][0]["first_text_ms"] is not None
    for name in ("tool", "compaction", "confirmation"):
        assert by_name[name][0]["first_text_ms"] is None
    RESULTS["groups"] = {
        "names": names,
        "model_keys": [g["key"] for g in by_name["model"]],
        "tool_names": [g["key"]["tool_name"] for g in by_name["tool"]],
    }


# ---------- 6. 状态计数、成功率、仅 completed 样本、零值与未知首正文 ----------

def test_status_counts_and_first_text() -> None:
    model = {"provider": "P6", "model": "M6", "call_kind": "normal"}
    t1 = identity(trace_id=uuid4_hex())
    t2 = identity(trace_id=uuid4_hex())
    records = [
        record(1, None, "agent_run", started_at=NOW - 1000, duration=10.0, attributes={"first_text_ms": 100.0}, **t1),
        record(2, 1, "model", started_at=NOW - 990, duration=5.0, attributes={**model, "first_text_ms": 0.0}, **t1),
        record(
            1,
            None,
            "agent_run",
            started_at=NOW - 900,
            duration=20.0,
            status="failed",
            attributes={"first_text_ms": 999.0},
            **t2,
        ),
        record(2, 1, "model", started_at=NOW - 890, duration=6.0, status="failed", attributes={**model, "first_text_ms": 7.0}, **t2),
        record(
            1,
            None,
            "agent_run",
            started_at=NOW - 800,
            duration=30.0,
            status="cancelled",
            attributes={"first_text_ms": 999.0},
            **identity(trace_id=uuid4_hex()),
        ),
        # 未知首正文：completed 但属性缺失，不入样本。
        record(1, None, "agent_run", started_at=NOW - 700, duration=40.0, **identity(trace_id=uuid4_hex())),
    ]
    path = write_database("status", records)
    by_name = groups_by_name(success(path, ["metrics"]))

    agent = by_name["agent_run"][0]
    assert agent["calls"] == 4
    assert agent["status_counts"] == {"completed": 2, "failed": 1, "cancelled": 1}
    assert agent["failure_rate"] == pytest.approx(0.25)
    assert agent["cancellation_rate"] == pytest.approx(0.25)
    # 耗时只取 completed：T1(10)、T4(40)。
    assert_distribution(agent["duration_ms"], samples=2, mean=25.0, p50=25.0, p95=38.5, maximum=40.0)
    # 首正文只取 completed 且属性存在的 T1：T2/T3 非 completed，T4 属性缺失。
    assert_distribution(agent["first_text_ms"], samples=1, mean=100.0, p50=100.0, p95=100.0, maximum=100.0)

    model_group = by_name["model"][0]
    assert model_group["calls"] == 2
    assert model_group["status_counts"] == {"completed": 1, "failed": 1, "cancelled": 0}
    assert model_group["failure_rate"] == pytest.approx(0.5)
    assert model_group["cancellation_rate"] == pytest.approx(0.0)
    assert_distribution(model_group["duration_ms"], samples=1, mean=5.0, p50=5.0, p95=5.0, maximum=5.0)
    # 零值首正文计为有效样本，失败调用不计入。
    assert_distribution(model_group["first_text_ms"], samples=1, mean=0.0, p50=0.0, p95=0.0, maximum=0.0)
    RESULTS["status"] = {
        "agent": agent["status_counts"],
        "agent_failure_rate": agent["failure_rate"],
        "agent_duration": agent["duration_ms"],
        "model_first_text": model_group["first_text_ms"],
    }


# ---------- 7. 耗时统计：空/单/奇/偶与 p50、inclusive p95、mean、max ----------

def test_duration_distributions() -> None:
    model = {"provider": "P7", "model": "M7", "call_kind": "normal"}
    r1 = identity(trace_id=uuid4_hex())
    r2 = identity(trace_id=uuid4_hex())
    r3 = identity(trace_id=uuid4_hex())
    r4 = identity(trace_id=uuid4_hex())
    records = [
        record(1, None, "agent_run", started_at=NOW - 1000, duration=10.0, attributes={"first_text_ms": 5.0}, **r1),
        record(2, 1, "model", started_at=NOW - 995, duration=10.0, attributes={**model, "first_text_ms": 1.0}, **r1),
        record(3, 1, "tool", started_at=NOW - 994, duration=1.0, attributes={"tool_call_id": "c1", "tool_name": "t"}, **r1),
        record(4, 1, "compaction", started_at=NOW - 993, duration=12.0, attributes={"reason": "threshold"}, **r1),
        record(1, None, "agent_run", started_at=NOW - 900, duration=20.0, attributes={"first_text_ms": 6.0}, **r2),
        record(2, 1, "model", started_at=NOW - 895, duration=20.0, attributes={**model, "first_text_ms": 2.0}, **r2),
        record(3, 1, "tool", started_at=NOW - 894, duration=2.0, attributes={"tool_call_id": "c2", "tool_name": "t"}, **r2),
        record(1, None, "agent_run", started_at=NOW - 800, duration=30.0, attributes={"first_text_ms": 7.0}, **r3),
        record(2, 1, "model", started_at=NOW - 795, duration=30.0, attributes={**model, "first_text_ms": 3.0}, **r3),
        record(3, 1, "tool", started_at=NOW - 794, duration=3.0, attributes={"tool_call_id": "c3", "tool_name": "t"}, **r3),
        record(1, None, "agent_run", started_at=NOW - 700, duration=40.0, attributes={"first_text_ms": 8.0}, **r4),
        record(2, 1, "model", started_at=NOW - 695, duration=40.0, attributes={**model, "first_text_ms": 4.0}, **r4),
        record(3, 1, "model", started_at=NOW - 694, duration=50.0, attributes={**model, "first_text_ms": 5.0}, **r4),
        record(
            1,
            None,
            "confirmation",
            started_at=NOW - 600,
            status="failed",
            attributes={"business_kind": "plan"},
            **identity(trace_id=uuid4_hex(), run_id=None),
        ),
    ]
    path = write_database("dist", records)
    by_name = groups_by_name(success(path, ["metrics"]))

    # 偶数样本。
    agent = by_name["agent_run"][0]
    assert_distribution(agent["duration_ms"], samples=4, mean=25.0, p50=25.0, p95=38.5, maximum=40.0)
    assert_distribution(agent["first_text_ms"], samples=4, mean=6.5, p50=6.5, p95=7.85, maximum=8.0)
    # 奇数样本。
    model_group = by_name["model"][0]
    assert_distribution(model_group["duration_ms"], samples=5, mean=30.0, p50=30.0, p95=48.0, maximum=50.0)
    assert_distribution(model_group["first_text_ms"], samples=5, mean=3.0, p50=3.0, p95=4.8, maximum=5.0)
    # 奇数小样本 [1,2,3]。
    tool_group = by_name["tool"][0]
    assert_distribution(tool_group["duration_ms"], samples=3, mean=2.0, p50=2.0, p95=2.9, maximum=3.0)
    assert tool_group["first_text_ms"] is None
    # 单样本。
    compaction = by_name["compaction"][0]
    assert_distribution(compaction["duration_ms"], samples=1, mean=12.0, p50=12.0, p95=12.0, maximum=12.0)
    # 空样本：该组无 completed。
    confirmation = by_name["confirmation"][0]
    assert_distribution(confirmation["duration_ms"], samples=0, mean=None, p50=None, p95=None, maximum=None)
    assert confirmation["status_counts"] == {"completed": 0, "failed": 1, "cancelled": 0}
    RESULTS["distributions"] = {
        "agent_duration": agent["duration_ms"],
        "model_duration": model_group["duration_ms"],
        "tool_duration": tool_group["duration_ms"],
        "compaction_duration": compaction["duration_ms"],
        "empty_duration": confirmation["duration_ms"],
    }


# ---------- 8. usage 四字段与失败/取消用量 ----------

def test_usage_fields() -> None:
    model = {"provider": "P8", "model": "M8", "call_kind": "normal"}
    shared = identity(trace_id=uuid4_hex())
    records = [
        record(1, None, "agent_run", started_at=NOW - 1000, **shared),
        record(
            2,
            1,
            "model",
            started_at=NOW - 990,
            attributes={**model, "input_tokens": 10, "output_tokens": 20, "cache_read_tokens": 3, "cache_write_tokens": 4},
            **shared,
        ),
        record(3, 1, "model", started_at=NOW - 980, attributes={**model, "input_tokens": 0}, **shared),
        record(4, 1, "model", started_at=NOW - 970, attributes={**model, "output_tokens": 5}, **shared),
        record(
            5,
            1,
            "model",
            started_at=NOW - 960,
            status="failed",
            attributes={**model, "input_tokens": 7, "output_tokens": 8, "cache_read_tokens": 1, "cache_write_tokens": 0},
            **shared,
        ),
        record(
            6,
            1,
            "model",
            started_at=NOW - 950,
            status="cancelled",
            attributes={**model, "input_tokens": 2, "output_tokens": 3},
            **shared,
        ),
    ]
    # 另一个模型组：只有 input 已知，其余 total 为 null。
    secondary_trace = identity(trace_id=uuid4_hex())
    records += [
        record(1, None, "agent_run", started_at=NOW - 800, **secondary_trace),
        record(
            2,
            1,
            "model",
            started_at=NOW - 790,
            attributes={"provider": "Q8", "model": "N8", "call_kind": "normal", "output_tokens": 9},
            **secondary_trace,
        ),
    ]
    path = write_database("usage", records)
    by_name = groups_by_name(success(path, ["metrics"]))
    primary = next(g for g in by_name["model"] if g["key"]["provider"] == "P8")
    usage = primary["usage"]
    assert usage["input_tokens"] == {"total": 19, "samples": 4}
    assert usage["output_tokens"] == {"total": 36, "samples": 4}
    assert usage["cache_read_tokens"] == {"total": 4, "samples": 2}
    assert usage["cache_write_tokens"] == {"total": 4, "samples": 2}
    secondary = next(g for g in by_name["model"] if g["key"]["provider"] == "Q8")
    assert secondary["usage"]["output_tokens"] == {"total": 9, "samples": 1}
    assert secondary["usage"]["input_tokens"] == {"total": None, "samples": 0}
    assert secondary["usage"]["cache_read_tokens"] == {"total": None, "samples": 0}
    RESULTS["usage"] = {"primary": {key: usage[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}, "secondary_input": secondary["usage"]["input_tokens"]}


# ---------- 9. 缓存命中率 ----------

def test_cache_hit_rate() -> None:
    model = {"provider": "P9", "model": "M9", "call_kind": "normal"}
    shared = identity(trace_id=uuid4_hex())
    records = [
        record(1, None, "agent_run", started_at=NOW - 1000, **shared),
        record(2, 1, "model", started_at=NOW - 990, attributes={**model, "input_tokens": 100, "cache_read_tokens": 40, "cache_write_tokens": 60}, **shared),
        record(3, 1, "model", started_at=NOW - 980, attributes={**model, "input_tokens": 50, "cache_read_tokens": 25, "cache_write_tokens": 25}, **shared),
        # cache_write 未知：不进共同样本，但 input/cache_read 仍各自计入字段汇总。
        record(4, 1, "model", started_at=NOW - 970, attributes={**model, "input_tokens": 10, "cache_read_tokens": 5}, **shared),
        # output 已知不影响资格。
        record(5, 1, "model", started_at=NOW - 960, attributes={**model, "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 1, "cache_write_tokens": 0}, **shared),
    ]
    other = identity(trace_id=uuid4_hex())
    records += [
        record(1, None, "agent_run", started_at=NOW - 900, **other),
        # 共同样本但分母为 0：命中率为 null。
        record(2, 1, "model", started_at=NOW - 890, attributes={"provider": "Q9", "model": "N9", "call_kind": "normal", "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}, **other),
    ]
    path = write_database("cache", records)
    by_name = groups_by_name(success(path, ["metrics"]))
    primary = next(g for g in by_name["model"] if g["key"]["provider"] == "P9")["usage"]
    assert primary["cache_samples"] == 3
    expected = 66 / 311
    assert primary["cache_hit_rate"] == pytest.approx(expected)
    per_call = (40 / 200 + 25 / 100 + 1 / 11) / 3
    assert abs(primary["cache_hit_rate"] - per_call) > 0.01
    assert primary["input_tokens"] == {"total": 170, "samples": 4}
    assert primary["cache_read_tokens"] == {"total": 71, "samples": 4}
    zero = next(g for g in by_name["model"] if g["key"]["provider"] == "Q9")["usage"]
    assert zero["cache_samples"] == 1
    assert zero["cache_hit_rate"] is None
    RESULTS["cache"] = {
        "primary_samples": primary["cache_samples"],
        "primary_hit_rate": primary["cache_hit_rate"],
        "per_call_average": per_call,
        "zero_denominator_hit_rate": zero["cache_hit_rate"],
    }


# ---------- 10. slow_calls ----------

def test_slow_calls_order_and_truncation() -> None:
    ta, tb, tc, te = trace_hex(1), trace_hex(2), trace_hex(3), trace_hex(5)
    tool = {"tool_name": "s"}
    records = [
        record(1, None, "agent_run", started_at=NOW - 9000, **identity(trace_id=ta)),
        record(2, 1, "tool", started_at=1000, duration=100.0, status="completed", attributes=tool, **identity(trace_id=ta)),
        record(5, 1, "tool", started_at=1000, duration=100.0, status="failed", attributes=tool, **identity(trace_id=ta)),
        record(1, None, "agent_run", started_at=NOW - 9000, **identity(trace_id=tb)),
        record(3, 1, "tool", started_at=1000, duration=100.0, status="cancelled", attributes=tool, **identity(trace_id=tb)),
        record(1, None, "agent_run", started_at=NOW - 9000, **identity(trace_id=tc)),
        record(4, 1, "tool", started_at=2000, duration=100.0, status="completed", attributes=tool, **identity(trace_id=tc)),
        record(1, None, "agent_run", started_at=NOW - 9000, **identity(trace_id=te)),
    ]
    for index, duration in enumerate(range(90, 69, -1), start=2):
        records.append(
            record(index, 1, "tool", started_at=NOW - 500, duration=float(duration), attributes=tool, **identity(trace_id=te))
        )
    path = write_database("slow", records)
    group = next(g for g in groups_by_name(success(path, ["metrics"]))["tool"])
    assert group["calls"] == 25
    slow = group["slow_calls"]
    assert len(slow) == 20
    # 四级键降序：duration 相同比 started_at（C 的 2000 在前），再比 trace_id（B>A）、span_id（5>2）。
    assert [(item["trace_id"], item["span_id"]) for item in slow[:4]] == [
        (tc, 4),
        (tb, 3),
        (ta, 5),
        (ta, 2),
    ]
    assert [item["duration_ms"] for item in slow] == sorted((item["duration_ms"] for item in slow), reverse=True)
    assert slow[4]["duration_ms"] == 90.0
    assert slow[-1]["duration_ms"] == 75.0
    assert {item["status"] for item in slow} == {"completed", "failed", "cancelled"}
    assert all(set(item) == {"trace_id", "span_id", "started_at", "duration_ms", "status"} for item in slow)
    RESULTS["slow_calls"] = {
        "calls": group["calls"],
        "returned": len(slow),
        "top_four": [(item["trace_id"], item["span_id"]) for item in slow[:4]],
        "last_duration_ms": slow[-1]["duration_ms"],
    }


# ---------- 11. 数据与结构故障 ----------

def test_data_and_schema_faults() -> None:
    faults = {}

    # attributes 非合法 JSON。
    path = empty_database("fault_json")
    run_id = uuid4_canonical()
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            _INSERT,
            (trace_hex(30), 1, None, uuid4_canonical(), run_id, uuid4_canonical(), "agent_run", NOW - 1000, NOW - 999, 1.0, "completed", None, '{"first_text_ms": 1.0}'),
        )
        connection.execute(
            _INSERT,
            (trace_hex(30), 2, 1, uuid4_canonical(), run_id, uuid4_canonical(), "turn", NOW - 998, NOW - 997, 1.0, "completed", None, "not json"),
        )
        connection.commit()
    finally:
        connection.close()
    faults["attributes_non_json"] = failure(path, ["metrics"])
    faults["attributes_non_json_show"] = failure(path, ["show", "--run-id", run_id])
    assert "JSONDecodeError" in faults["attributes_non_json"]
    assert "JSONDecodeError" in faults["attributes_non_json_show"]

    # status 非法。
    path = empty_database("fault_status")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            _INSERT,
            (trace_hex(31), 1, None, uuid4_canonical(), uuid4_canonical(), uuid4_canonical(), "agent_run", NOW - 1000, NOW - 999, 1.0, "completed", None, "{}"),
        )
        connection.execute(
            _INSERT,
            (trace_hex(31), 2, 1, uuid4_canonical(), uuid4_canonical(), uuid4_canonical(), "turn", NOW - 998, NOW - 997, 1.0, "unknown", None, "{}"),
        )
        connection.commit()
    finally:
        connection.close()
    faults["status_invalid"] = failure(path, ["metrics"])
    assert "ValidationError" in faults["status_invalid"]

    # 属性越界（input_tokens 为负）：JSON 合法，Span 属性校验失败。
    path = empty_database("fault_attr")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            _INSERT,
            (trace_hex(32), 1, None, uuid4_canonical(), uuid4_canonical(), uuid4_canonical(), "agent_run", NOW - 1000, NOW - 999, 1.0, "completed", None, "{}"),
        )
        connection.execute(
            _INSERT,
            (trace_hex(32), 2, 1, uuid4_canonical(), uuid4_canonical(), uuid4_canonical(), "model", NOW - 998, NOW - 997, 1.0, "completed", None, '{"provider": "p", "model": "m", "call_kind": "normal", "input_tokens": -5}'),
        )
        connection.commit()
    finally:
        connection.close()
    faults["attribute_out_of_range"] = failure(path, ["metrics"])
    assert "ValidationError" in faults["attribute_out_of_range"]

    # 结构不兼容：spans 表存在但列不同。
    path = database("fault_schema")
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE spans (trace_id TEXT, span_id INTEGER)")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()
    faults["columns_mismatch"] = failure(path, ["metrics"])
    assert "no such column" in faults["columns_mismatch"]

    # 结构不兼容：spans 表缺失。
    path = database("fault_notable")
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE other (x INTEGER)")
        connection.commit()
    finally:
        connection.close()
    faults["table_missing"] = failure(path, ["metrics"])
    assert "no such table" in faults["table_missing"]

    # 数据库不存在：只读打开失败，且不得创建文件。
    path = DBS / "fault_missing.db"
    path.unlink(missing_ok=True)
    faults["database_missing"] = failure(path, ["metrics"])
    assert "unable to open database file" in faults["database_missing"]
    assert not path.exists()
    RESULTS["faults"] = faults


# ---------- 12. 连接释放、只读、不创建缺失库 ----------

def test_connection_release_and_readonly() -> None:
    path = write_database(
        "release",
        [record(1, None, "agent_run", started_at=NOW - 1000, attributes={"first_text_ms": 1.0})],
    )
    before_bytes = path.read_bytes()
    read_before = _read_rows(path)
    payload = success(path, ["metrics"])
    assert payload["groups"][0]["calls"] == 1
    after_bytes = path.read_bytes()
    read_after = _read_rows(path)
    assert before_bytes == after_bytes
    assert read_before == read_after

    # 成功路径无残留句柄：报告运行后可改名。
    renamed = path.with_name("release_renamed.db")
    renamed.unlink(missing_ok=True)
    path.rename(renamed)
    assert renamed.exists()
    renamed.rename(path)

    # 失败路径同样释放连接。
    fault = database("release_fault")
    connection = sqlite3.connect(fault)
    try:
        connection.execute("CREATE TABLE spans (trace_id TEXT)")
        connection.commit()
    finally:
        connection.close()
    failure(fault, ["metrics"])
    fault_renamed = fault.with_name("release_fault_renamed.db")
    fault_renamed.unlink(missing_ok=True)
    fault.rename(fault_renamed)
    assert fault_renamed.exists()
    fault_renamed.rename(fault)

    # 不创建缺失数据库。
    missing = DBS / "release_missing.db"
    missing.unlink(missing_ok=True)
    failure(missing, ["metrics"])
    assert not missing.exists()
    RESULTS["release"] = {
        "bytes_unchanged": before_bytes == after_bytes,
        "rows_unchanged": read_before == read_after,
        "rename_after_success": True,
        "rename_after_failure": True,
        "missing_not_created": not missing.exists(),
    }


def _read_rows(path: Path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT trace_id, span_id, name, status, attributes FROM spans ORDER BY trace_id, span_id"
        ).fetchall()
    finally:
        connection.close()


# ---------- 14. 真实行形状兼容 ----------

def test_real_row_shapes() -> None:
    run_id = uuid4_canonical()
    operation_id = uuid4_canonical()
    agent = identity(trace_id=uuid4_hex(), run_id=run_id)
    records = [
        record(1, None, "agent_run", started_at=NOW - 1000, duration=120.0, attributes={"first_text_ms": 12.5}, **agent),
        record(2, 1, "turn", started_at=NOW - 990, **agent),
        record(
            3,
            2,
            "model",
            started_at=NOW - 980,
            duration=30.0,
            attributes={
                "provider": "openai",
                "model": "gpt-x",
                "call_kind": "normal",
                "stop_reason": "stop",
                "first_text_ms": 30.0,
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 20,
                "cache_write_tokens": 10,
            },
            **agent,
        ),
        record(
            4,
            2,
            "tool",
            started_at=NOW - 970,
            duration=5.0,
            attributes={"tool_call_id": "call_1", "tool_name": "get_weather", "execution_ms": 5.0, "postprocess_ms": 1.0},
            **agent,
        ),
        record(5, 1, "compaction", started_at=NOW - 960, duration=8.0, attributes={"reason": "overflow"}, **agent),
    ]
    confirmation = identity(trace_id=uuid4_hex(), run_id=None, operation_id=operation_id)
    records.append(
        record(1, None, "confirmation", started_at=NOW - 500, duration=3.0, attributes={"business_kind": "workout"}, **confirmation)
    )
    path = write_database("shape", records)
    payload = success(path, ["metrics"])
    names = [g["key"]["name"] for g in payload["groups"]]
    assert names == ["agent_run", "model", "tool", "compaction", "confirmation"]
    agent_group = groups_by_name(payload)["agent_run"][0]
    assert_distribution(agent_group["duration_ms"], samples=1, mean=120.0, p50=120.0, p95=120.0, maximum=120.0)
    assert agent_group["usage"] is None

    chain = success(path, ["show", "--run-id", run_id])
    assert [span["name"] for span in chain["spans"]] == ["agent_run", "turn", "model", "tool", "compaction"]
    confirmed = success(path, ["show", "--operation-id", operation_id])
    assert [span["name"] for span in confirmed["spans"]] == ["confirmation"]
    RESULTS["real_shapes"] = {"group_names": names, "agent_chain": [span["name"] for span in chain["spans"]]}


# ---------- 独立隔离机制核查 ----------

def test_isolation_patch_used() -> None:
    original = telemetry.trace_database_path
    patched = patch_trace_database("trace-report")
    try:
        assert str(patched).startswith(str(ROOT))
        assert telemetry.trace_database_path() == patched
    finally:
        telemetry.trace_database_path = original
    assert telemetry.trace_database_path() == BACKEND / "data" / "trace.db"
    RESULTS["isolation_patch"] = {"patched": str(patched), "root": str(ROOT)}


# ---------- 15. 显式 null 与省略同按未知处理 ----------

# 报告契约：属性缺失与显式 null 等价，均为“未知”，不进任何样本；0 仍是已知值。
NULL_DISTRIBUTION = {"samples": 0, "mean": None, "p50": None, "p95": None, "max": None}
NULL_USAGE = {"total": None, "samples": 0}


def model_attributes(provider: str, model: str, **extra) -> dict:
    return {"provider": provider, "model": model, "call_kind": "normal", **extra}


def null_record(span_id, parent, name, *, started_at, attributes, **fields) -> SpanRecord:
    # 显式 null 必须在真实 SpanRecord 上原样保留：既不丢弃键，也不转成省略。
    built = record(span_id, parent, name, started_at=started_at, attributes=attributes, **fields)
    for key, value in attributes.items():
        assert key in built.attributes, (key, built.attributes)
        if value is None:
            assert built.attributes[key] is None, (key, built.attributes)
        else:
            assert built.attributes[key] == value, (key, built.attributes)
    return built


def persisted_nulls(path, trace_id, expected) -> dict:
    # 真实存储往返：TraceStore 写库后读回，显式 null 仍为 None。
    async def scenario():
        store = await open_trace_store(path)
        try:
            return await store.read_trace(trace_id)
        finally:
            await store.close()

    stored = {item.span_id: item for item in run(scenario())}
    observed = {}
    for span_id, fields in expected.items():
        for field in fields:
            assert field in stored[span_id].attributes, (span_id, field, stored[span_id].attributes)
            assert stored[span_id].attributes[field] is None, (span_id, field, stored[span_id].attributes)
            observed[f"{span_id}.{field}"] = None
    return observed


def model_group(payload: dict, provider: str) -> dict:
    return next(
        group for group in groups_by_name(payload)["model"] if group["key"]["provider"] == provider
    )


def test_usage_explicit_null_fields() -> None:
    # 四字段各自显式 null：该字段 {total: null, samples: 0}，同组其余字段不受影响；
    # 失败/取消调用中的真实用量照常计入。
    t_input = identity(trace_id=uuid4_hex())
    t_known = identity(trace_id=uuid4_hex())
    t_output = identity(trace_id=uuid4_hex())
    t_read = identity(trace_id=uuid4_hex())
    t_write = identity(trace_id=uuid4_hex())
    records = [
        null_record(1, None, "agent_run", started_at=NOW - 1000, attributes={}, **t_input),
        null_record(
            2,
            1,
            "model",
            started_at=NOW - 990,
            attributes=model_attributes(
                "U_input",
                "M",
                input_tokens=None,
                output_tokens=6,
                cache_read_tokens=2,
                cache_write_tokens=4,
            ),
            **t_input,
        ),
        # 显式 null 的记录与失败/取消记录同组：null 不计入，失败/取消的真实用量计入。
        null_record(1, None, "agent_run", started_at=NOW - 900, attributes={}, **t_known),
        null_record(
            2,
            1,
            "model",
            started_at=NOW - 890,
            attributes=model_attributes("U_known", "M", input_tokens=None, output_tokens=5),
            **t_known,
        ),
        null_record(
            3,
            1,
            "model",
            started_at=NOW - 880,
            status="failed",
            attributes=model_attributes("U_known", "M", input_tokens=7, output_tokens=8),
            **t_known,
        ),
        null_record(
            4,
            1,
            "model",
            started_at=NOW - 870,
            status="cancelled",
            attributes=model_attributes("U_known", "M", input_tokens=2, output_tokens=3),
            **t_known,
        ),
        null_record(1, None, "agent_run", started_at=NOW - 800, attributes={}, **t_output),
        null_record(
            2,
            1,
            "model",
            started_at=NOW - 790,
            attributes=model_attributes(
                "U_output",
                "M",
                output_tokens=None,
                input_tokens=11,
                cache_read_tokens=3,
                cache_write_tokens=1,
            ),
            **t_output,
        ),
        null_record(1, None, "agent_run", started_at=NOW - 700, attributes={}, **t_read),
        null_record(
            2,
            1,
            "model",
            started_at=NOW - 690,
            attributes=model_attributes(
                "U_read", "M", cache_read_tokens=None, input_tokens=9, cache_write_tokens=3
            ),
            **t_read,
        ),
        null_record(1, None, "agent_run", started_at=NOW - 600, attributes={}, **t_write),
        null_record(
            2,
            1,
            "model",
            started_at=NOW - 590,
            attributes=model_attributes(
                "U_write", "M", cache_write_tokens=None, input_tokens=8, cache_read_tokens=2
            ),
            **t_write,
        ),
    ]
    path = write_database("null_usage", records)
    payload = success(path, ["metrics"])

    nulled_input = model_group(payload, "U_input")["usage"]
    assert nulled_input["input_tokens"] == NULL_USAGE
    assert nulled_input["output_tokens"] == {"total": 6, "samples": 1}
    assert nulled_input["cache_read_tokens"] == {"total": 2, "samples": 1}
    assert nulled_input["cache_write_tokens"] == {"total": 4, "samples": 1}
    assert nulled_input["cache_samples"] == 0
    assert nulled_input["cache_hit_rate"] is None

    known = model_group(payload, "U_known")["usage"]
    assert known["input_tokens"] == {"total": 9, "samples": 2}
    assert known["output_tokens"] == {"total": 16, "samples": 3}

    nulled_output = model_group(payload, "U_output")["usage"]
    assert nulled_output["output_tokens"] == NULL_USAGE
    assert nulled_output["input_tokens"] == {"total": 11, "samples": 1}
    assert nulled_output["cache_samples"] == 1
    assert nulled_output["cache_hit_rate"] == pytest.approx(3 / 15)

    nulled_read = model_group(payload, "U_read")["usage"]
    assert nulled_read["cache_read_tokens"] == NULL_USAGE
    assert nulled_read["input_tokens"] == {"total": 9, "samples": 1}
    assert nulled_read["cache_write_tokens"] == {"total": 3, "samples": 1}
    assert nulled_read["cache_samples"] == 0
    assert nulled_read["cache_hit_rate"] is None

    nulled_write = model_group(payload, "U_write")["usage"]
    assert nulled_write["cache_write_tokens"] == NULL_USAGE
    assert nulled_write["cache_read_tokens"] == {"total": 2, "samples": 1}
    assert nulled_write["cache_samples"] == 0
    assert nulled_write["cache_hit_rate"] is None

    persisted = persisted_nulls(path, t_input["trace_id"], {2: {"input_tokens"}})
    persisted.update(persisted_nulls(path, t_output["trace_id"], {2: {"output_tokens"}}))
    persisted.update(persisted_nulls(path, t_read["trace_id"], {2: {"cache_read_tokens"}}))
    persisted.update(persisted_nulls(path, t_write["trace_id"], {2: {"cache_write_tokens"}}))
    RESULTS["null_usage"] = {
        "input_null": nulled_input["input_tokens"],
        "input_known_failed_cancelled": known["input_tokens"],
        "output_null": nulled_output["output_tokens"],
        "cache_read_null": nulled_read["cache_read_tokens"],
        "cache_write_null": nulled_write["cache_write_tokens"],
        "persisted_nulls": persisted,
    }


def test_cache_common_samples_explicit_null() -> None:
    # 缓存共同样本要求 input/cache_read/cache_write 三项都已知；
    # 任一为显式 null（或省略）都排除该记录，命中率按剩余共同样本计算或为 null。
    t_primary = identity(trace_id=uuid4_hex())
    t_only_null = identity(trace_id=uuid4_hex())
    t_omitted = identity(trace_id=uuid4_hex())
    records = [
        null_record(1, None, "agent_run", started_at=NOW - 1000, attributes={}, **t_primary),
        null_record(
            2,
            1,
            "model",
            started_at=NOW - 990,
            attributes=model_attributes("C1", "M", input_tokens=100, cache_read_tokens=40, cache_write_tokens=60),
            **t_primary,
        ),
        null_record(
            3,
            1,
            "model",
            started_at=NOW - 980,
            attributes=model_attributes("C1", "M", input_tokens=50, cache_read_tokens=25, cache_write_tokens=25),
            **t_primary,
        ),
        null_record(
            4,
            1,
            "model",
            started_at=NOW - 970,
            attributes=model_attributes("C1", "M", input_tokens=None, cache_read_tokens=5, cache_write_tokens=5),
            **t_primary,
        ),
        null_record(
            5,
            1,
            "model",
            started_at=NOW - 960,
            attributes=model_attributes("C1", "M", input_tokens=10, cache_read_tokens=None, cache_write_tokens=5),
            **t_primary,
        ),
        null_record(
            6,
            1,
            "model",
            started_at=NOW - 950,
            attributes=model_attributes("C1", "M", input_tokens=10, cache_read_tokens=5, cache_write_tokens=None),
            **t_primary,
        ),
        null_record(1, None, "agent_run", started_at=NOW - 900, attributes={}, **t_only_null),
        null_record(
            2,
            1,
            "model",
            started_at=NOW - 890,
            attributes=model_attributes("C2", "M", input_tokens=None, cache_read_tokens=5, cache_write_tokens=5),
            **t_only_null,
        ),
        null_record(1, None, "agent_run", started_at=NOW - 800, attributes={}, **t_omitted),
        null_record(
            2,
            1,
            "model",
            started_at=NOW - 790,
            attributes=model_attributes("C3", "M", input_tokens=20, cache_read_tokens=10),
            **t_omitted,
        ),
        null_record(
            3,
            1,
            "model",
            started_at=NOW - 780,
            attributes=model_attributes("C3", "M", input_tokens=30, cache_read_tokens=15, cache_write_tokens=5),
            **t_omitted,
        ),
    ]
    path = write_database("null_cache", records)
    payload = success(path, ["metrics"])

    primary = model_group(payload, "C1")["usage"]
    # 6 条记录中 3 条带显式 null，全部移出共同样本，只剩 2 条。
    assert primary["cache_samples"] == 2
    assert primary["cache_hit_rate"] == pytest.approx(65 / 300)
    assert primary["input_tokens"] == {"total": 170, "samples": 4}
    assert primary["cache_read_tokens"] == {"total": 75, "samples": 4}
    assert primary["cache_write_tokens"] == {"total": 95, "samples": 4}
    assert primary["output_tokens"] == NULL_USAGE

    only_null = model_group(payload, "C2")["usage"]
    assert only_null["cache_samples"] == 0
    assert only_null["cache_hit_rate"] is None
    assert only_null["cache_read_tokens"] == {"total": 5, "samples": 1}

    omitted = model_group(payload, "C3")["usage"]
    assert omitted["cache_samples"] == 1
    assert omitted["cache_hit_rate"] == pytest.approx(15 / 50)

    persisted = persisted_nulls(
        path, t_primary["trace_id"], {4: {"input_tokens"}, 5: {"cache_read_tokens"}, 6: {"cache_write_tokens"}}
    )
    RESULTS["null_cache"] = {
        "primary_samples": primary["cache_samples"],
        "primary_hit_rate": primary["cache_hit_rate"],
        "only_null_samples": only_null["cache_samples"],
        "only_null_hit_rate": only_null["cache_hit_rate"],
        "omitted_samples": omitted["cache_samples"],
        "persisted_nulls": persisted,
    }


def test_first_text_explicit_null_and_omitted() -> None:
    # first_text_ms 显式 null（agent_run 与 model）与省略同按未知处理：该组样本 0、四项 null。
    t_null = identity(trace_id=uuid4_hex())
    t_mixed = identity(trace_id=uuid4_hex())
    null_path = write_database(
        "first_text_null",
        [
            null_record(1, None, "agent_run", started_at=NOW - 1000, attributes={"first_text_ms": None}, **t_null),
            null_record(
                2,
                1,
                "model",
                started_at=NOW - 990,
                attributes=model_attributes("F1", "M", first_text_ms=None),
                **t_null,
            ),
            # 同组混合：显式 null 被排除，真实值仍计入。
            null_record(1, None, "agent_run", started_at=NOW - 900, attributes={}, **t_mixed),
            null_record(
                2,
                1,
                "model",
                started_at=NOW - 890,
                attributes=model_attributes("F1m", "M", first_text_ms=None),
                **t_mixed,
            ),
            null_record(
                3,
                1,
                "model",
                started_at=NOW - 880,
                attributes=model_attributes("F1m", "M", first_text_ms=25.0),
                **t_mixed,
            ),
        ],
    )
    payload = success(null_path, ["metrics"])
    # agent_run 组：一个根显式 null、一个根省略，都非样本。
    assert groups_by_name(payload)["agent_run"][0]["first_text_ms"] == NULL_DISTRIBUTION
    assert model_group(payload, "F1")["first_text_ms"] == NULL_DISTRIBUTION
    assert_distribution(
        model_group(payload, "F1m")["first_text_ms"], samples=1, mean=25.0, p50=25.0, p95=25.0, maximum=25.0
    )

    t_omitted = identity(trace_id=uuid4_hex())
    omitted_path = write_database(
        "first_text_omitted",
        [
            record(1, None, "agent_run", started_at=NOW - 1000, attributes={}, **t_omitted),
            record(
                2,
                1,
                "model",
                started_at=NOW - 990,
                attributes=model_attributes("F2", "M"),
                **t_omitted,
            ),
        ],
    )
    omitted_payload = success(omitted_path, ["metrics"])
    assert groups_by_name(omitted_payload)["agent_run"][0]["first_text_ms"] == NULL_DISTRIBUTION
    assert model_group(omitted_payload, "F2")["first_text_ms"] == NULL_DISTRIBUTION

    persisted = persisted_nulls(null_path, t_null["trace_id"], {1: {"first_text_ms"}, 2: {"first_text_ms"}})
    persisted.update(persisted_nulls(omitted_path, t_omitted["trace_id"], {1: set(), 2: set()}))
    RESULTS["null_first_text"] = {
        "explicit_null_agent": NULL_DISTRIBUTION,
        "explicit_null_model": NULL_DISTRIBUTION,
        "mixed_known": model_group(payload, "F1m")["first_text_ms"],
        "omitted_agent": NULL_DISTRIBUTION,
        "persisted_nulls": persisted,
    }


def test_zero_values_are_valid_samples() -> None:
    # 0 是已知值：token 0 计入 total/samples，first_text_ms=0 计入统计。
    t_zero = identity(trace_id=uuid4_hex())
    t_tokens = identity(trace_id=uuid4_hex())
    records = [
        record(1, None, "agent_run", started_at=NOW - 1000, attributes={"first_text_ms": 0.0}, **t_zero),
        record(
            2,
            1,
            "model",
            started_at=NOW - 990,
            attributes=model_attributes("Z1", "M", first_text_ms=0.0),
            **t_zero,
        ),
        record(
            3,
            1,
            "model",
            started_at=NOW - 980,
            attributes=model_attributes("Z1", "M", first_text_ms=10.0),
            **t_zero,
        ),
        record(1, None, "agent_run", started_at=NOW - 900, attributes={}, **t_tokens),
        record(
            2,
            1,
            "model",
            started_at=NOW - 890,
            attributes=model_attributes(
                "Z2", "M", input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
            ),
            **t_tokens,
        ),
        record(
            3,
            1,
            "model",
            started_at=NOW - 880,
            attributes=model_attributes(
                "Z2", "M", input_tokens=0, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0
            ),
            **t_tokens,
        ),
    ]
    path = write_database("zeros", records)
    payload = success(path, ["metrics"])

    # agent_run：唯一已知首正文为 0；省略根不计入，所以样本数 1（若 0 被当作未知则为 0）。
    assert_distribution(
        groups_by_name(payload)["agent_run"][0]["first_text_ms"],
        samples=1,
        mean=0.0,
        p50=0.0,
        p95=0.0,
        maximum=0.0,
    )
    assert_distribution(
        model_group(payload, "Z1")["first_text_ms"],
        samples=2,
        mean=5.0,
        p50=5.0,
        p95=9.5,
        maximum=10.0,
    )

    zero_usage = model_group(payload, "Z2")["usage"]
    assert zero_usage["input_tokens"] == {"total": 0, "samples": 2}
    assert zero_usage["output_tokens"] == {"total": 5, "samples": 2}
    assert zero_usage["cache_read_tokens"] == {"total": 0, "samples": 2}
    assert zero_usage["cache_write_tokens"] == {"total": 0, "samples": 2}
    # 三项都是已知 0：仍是有效共同样本，命中率因分母为 0 而为 null。
    assert zero_usage["cache_samples"] == 2
    assert zero_usage["cache_hit_rate"] is None
    RESULTS["zero_samples"] = {
        "agent_first_text": groups_by_name(payload)["agent_run"][0]["first_text_ms"],
        "model_first_text": model_group(payload, "Z1")["first_text_ms"],
        "zero_usage": {key: zero_usage[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")},
        "zero_cache_samples": zero_usage["cache_samples"],
    }


# ---------- 13. 生产数据路径基线 ----------

def production_snapshot() -> tuple:
    entries = []
    for path in sorted((BACKEND / "data").rglob("*")):
        if "__pycache__" in path.parts:
            continue
        stat = path.stat()
        entries.append((str(path.relative_to(BACKEND)), stat.st_size, stat.st_mtime_ns))
    return tuple(entries)


def verify_production_paths_untouched(snapshot: tuple) -> dict:
    assert telemetry.trace_database_path() == BACKEND / "data" / "trace.db"
    path = BACKEND / "data" / "trace.db"
    existed = path.exists()
    assert current_data_snapshot() == snapshot
    return {"trace_database_existed": existed, "entries": len(snapshot)}


def current_data_snapshot() -> tuple:
    return production_snapshot()


def check() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    DBS.mkdir(parents=True, exist_ok=True)
    snapshot = production_snapshot()
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
    RESULTS["production_baseline"] = verify_production_paths_untouched(snapshot)
    EVIDENCE.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        "Trace 本地报告：空库与合法无匹配、根时间窗口包含/排除与单端默认与会话过滤、"
        "show 两类身份定位与 span_id 升序与会话不符与过期根、参数/非法 UUID/非整数毫秒/空反向越界窗口真实失败、"
        "五类统计组齐全与 model 三维分组与工具未知名称归 null 与 turn 排除、状态计数与成功率与仅 completed 耗时样本、"
        "耗时空/单/奇/偶的 mean/p50/inclusive p95/max、usage 四字段零值与未知与失败取消用量、"
        "缓存共同样本与整体命中率、slow_calls 截断 20 与四级键降序与三种终态、"
        "数据损坏与属性/状态校验失败与结构不兼容与库缺失真实失败、成功与失败路径连接释放与只读与不创建缺失库、"
        "真实行形状兼容、隔离路径写入且生产数据路径基线零改动、"
        "显式 null 与省略同按未知处理（四字段用量各自 null 与其余字段不受影响与失败取消用量仍计入、"
        "缓存共同样本三项任一 null 或省略即排除并按剩余样本计算、first_text 显式 null 与省略样本 0、"
        "0 仍为有效样本）通过"
    )


if __name__ == "__main__":
    check()
