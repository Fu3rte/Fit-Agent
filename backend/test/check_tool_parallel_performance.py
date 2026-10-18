import asyncio
import json
import platform
import subprocess
import sys
from pathlib import Path
from statistics import quantiles
from tempfile import TemporaryDirectory
from time import perf_counter, time_ns
from types import SimpleNamespace
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop
from app.agent.budget import RequestBudget
from app.agent.config import AgentLoopConfig
from app.agent.tool import run_tool_batch
from app.agent.tools.files import create_file_tools
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from app.application.session.service import SessionService
from app.domain.session.models import SendCommand, SendRequest, SessionMessageEntry
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.check_tool_scheduling import single, usage
from test.p0_contract import BenchmarkResult
from test.regression_support import (
    TEST_SESSION,
    session_workspace,
    temporary_root,
    text,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = PROJECT_ROOT / "temp" / "p0-evaluation" / "tool-parallel"
FILES = 64
LINES = 800
PAIRS = 30
WARMUP = 2
FIRST_MARKER = "1: line-00000"
LAST_MARKER = f"{LINES}: line-{LINES - 1:05d}"
SCOPE = "固定文件读取负载的工具调度性能"
MODEL = SimpleNamespace(
    api="openai-completions",
    provider="openai",
    model="perf-model",
    base_url="http://unused",
    api_key="unused",
)


def summarize(values: list[float]) -> dict:
    cuts = quantiles(values, n=100, method="inclusive")
    return {"n": len(values), "p50": cuts[49], "p95": cuts[94]}


def batch_timings(result) -> dict:
    # 批次与首结果耗时直接取真实调度器返回的时间戳，单位毫秒。
    first = None
    if result.first_result_at is not None:
        first = (result.first_result_at - result.prepare_started_at) * 1000
    return {
        "batch_ms": (result.finalized_at - result.prepare_started_at) * 1000,
        "first_ms": first,
    }


def correctness_of(messages: list[ToolResultMessage]) -> dict:
    # 每个结果必须非错误且完整覆盖首行与末行，构成正确性判定词汇。
    details = []
    for position, message in enumerate(messages):
        body = text(message)
        details.append(
            {
                "index": position,
                "tool_call_id": message.tool_call_id,
                "is_error": message.is_error,
                "has_first_line": FIRST_MARKER in body,
                "has_last_line": LAST_MARKER in body,
            }
        )
    correct = sum(
        1
        for item in details
        if not item["is_error"] and item["has_first_line"] and item["has_last_line"]
    )
    errors = sum(1 for item in details if item["is_error"])
    return {"results": len(details), "correct": correct, "errors": errors, "details": details}


def git_state() -> dict:
    def run(arguments: list[str]) -> str:
        completed = subprocess.run(
            ["git", *arguments], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    return {"commit": run(["rev-parse", "HEAD"]), "status_porcelain": run(["status", "--porcelain"])}


def build_files(tmp_root: Path | None = None) -> tuple[TemporaryDirectory, Path, str, list[Path]]:
    # 文件工具按统一 tmp 根解析相对路径，测量文件位于当前会话 workspace 之下。
    # 传入 tmp_root 时微基准临时根落在实验目录内，避免写入系统临时目录。
    directory = TemporaryDirectory(dir=None if tmp_root is None else str(tmp_root))
    root = Path(directory.name).resolve()
    workspace, prefix = session_workspace(root)
    payload = "".join(f"line-{index:05d} " + "x" * 69 + "\n" for index in range(LINES))
    paths = []
    for index in range(FILES):
        path = workspace / f"f{index:02d}.txt"
        path.write_text(payload, encoding="utf-8")
        paths.append(path)
    return directory, root, prefix, paths


def build_calls(prefix: str, paths: list[Path]) -> list[ToolCall]:
    return [
        ToolCall(
            type="toolCall",
            id=f"c{index:02d}",
            name="read",
            arguments={"path": f"{prefix}/{path.name}", "limit": LINES},
        )
        for index, path in enumerate(paths)
    ]


async def noop(_context, _signal):
    return None


async def run_batch(mode, calls, tools, declared) -> tuple[object, list[dict]]:
    # 直接驱动真实工具调度器 run_tool_batch，逐工具记录执行与后处理耗时。
    finalized: list[dict] = []

    async def collect(item):
        finalized.append(
            {
                "tool_call_id": item.message.tool_call_id,
                "exec_ms": item.duration_ms,
                "post_ms": item.postprocess_ms,
            }
        )

    result = await run_tool_batch(
        calls,
        tools=tools,
        declared=declared,
        execution_mode=mode,
        after_tool_call=noop,
        on_tool_finalized=collect,
    )
    assert result.failure is None, repr(result.failure)
    return result, finalized


def sample_of(mode: str, pair: int, order: int, result, finalized: list[dict]) -> dict:
    timings = batch_timings(result)
    verdict = correctness_of(result.messages)
    return {
        "pair": pair,
        "mode": mode,
        "order": order,
        "batch_ms": round(timings["batch_ms"], 4),
        "first_ms": None if timings["first_ms"] is None else round(timings["first_ms"], 4),
        "result_count": len(result.messages),
        "results": [
            {
                "tool_call_id": item["tool_call_id"],
                "exec_ms": None if item["exec_ms"] is None else round(item["exec_ms"], 4),
                "post_ms": None if item["post_ms"] is None else round(item["post_ms"], 4),
            }
            for item in finalized
        ],
        "correct": verdict["correct"],
        "errors": verdict["errors"],
    }


async def run_once(mode, system, tools, tool_use, stop):
    events = []
    count = {"value": 0}

    async def emit(event):
        events.append(event)

    def stream_fn(_model_spec, _context, _options):
        count["value"] += 1
        return single(tool_use if count["value"] == 1 else stop)

    config = AgentLoopConfig(
        model=MODEL,
        max_turns=4,
        request_budget=RequestBudget(),
        tool_execution=mode,
        after_tool_call=noop,
    )
    messages = await run_agent_loop(
        [UserMessage(role="user", content="读取全部文件", timestamp=0)],
        {"messages": [system], "tools": tools},
        config,
        emit,
        stream_fn=stream_fn,
    )
    turn = next(
        event
        for event in events
        if event["type"] == "turn_end" and event["tool_first_result_at"] is not None
    )
    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    assert len(results) == FILES
    return {
        "batch_ms": (turn["tool_finalized_at"] - turn["tool_prepare_started_at"]) * 1000,
        "first_ms": (turn["tool_first_result_at"] - turn["tool_prepare_started_at"]) * 1000,
    }, results


def build_loop_inputs(read, calls: list[ToolCall]) -> tuple[object, object, object, dict]:
    tools = {"read": read}
    system = SystemMessage(
        role="system", content="", tools_added=[read.definition()], timestamp=0
    )
    tool_use = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="读取全部文件"), *calls],
        api=MODEL.api,
        provider=MODEL.provider,
        model=MODEL.model,
        usage=usage(),
        stop_reason="toolUse",
        timestamp=0,
    )
    stop = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="完成")],
        api=MODEL.api,
        provider=MODEL.provider,
        model=MODEL.model,
        usage=usage(),
        stop_reason="stop",
        timestamp=0,
    )
    return system, tool_use, stop, tools


def loop_integration(read, calls: list[ToolCall]) -> tuple[dict, list[ToolResultMessage]]:
    # 真实 agent loop 路径：验证调度器经 loop 集成后同样返回全部正确结果。
    system, tool_use, stop, tools = build_loop_inputs(read, calls)
    evidence = {}
    parallel_results: list[ToolResultMessage] = []
    for mode in ("sequential", "parallel"):
        outcome, results = asyncio.run(run_once(mode, system, tools, tool_use, stop))
        verdict = correctness_of(results)
        evidence[mode] = {
            "batch_ms": round(outcome["batch_ms"], 4),
            "first_ms": round(outcome["first_ms"], 4),
            "correct": verdict["correct"],
            "errors": verdict["errors"],
            "results": verdict["results"],
        }
        if mode == "parallel":
            parallel_results = results
    return evidence, parallel_results


async def persist(results: list[ToolResultMessage], calls: list[ToolCall], read, tmp_root: Path | None = None) -> dict:
    # 传入手工隔离根时持久化库落在同一实验目录内；否则沿用测试专用目录。
    base = temporary_root("tool-parallel-perf") if tmp_root is None else Path(tmp_root)
    base.mkdir(parents=True, exist_ok=True)
    database = await open_database(base / f"persist-{uuid4().hex}.db")
    try:
        service = SessionService(SqliteSessionRepository(database))
        session_id = str(uuid4())
        await service.create_session(session_id, "并行持久化")
        system = SystemMessage(
            role="system", content="", tools_added=[read.definition()], timestamp=0
        )
        send = await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=session_id,
                request=SendRequest(text="读取文件"),
            ),
            system_message=system,
        )
        position = {"id": send.run.request_entry_id}
        assistant = AssistantMessage(
            role="assistant",
            content=[TextContent(type="text", text="读取"), *calls],
            api=MODEL.api,
            provider=MODEL.provider,
            model=MODEL.model,
            usage=usage(),
            stop_reason="toolUse",
            timestamp=0,
        )
        node = str(uuid4())
        await service.entries.append_entry(
            SessionMessageEntry(
                session_id=session_id,
                id=node,
                parent_id=position["id"],
                run_id=send.run.id,
                type="message",
                messages=[assistant],
                created_at=time_ns() // 1_000_000,
            )
        )
        position["id"] = node
        started = perf_counter()
        for message in results:
            child = str(uuid4())
            outcome = await service.entries.append_entry(
                SessionMessageEntry(
                    session_id=session_id,
                    id=child,
                    parent_id=position["id"],
                    run_id=send.run.id,
                    type="message",
                    messages=[message],
                    created_at=time_ns() // 1_000_000,
                )
            )
            assert not outcome.credential_detected
            position["id"] = child
        elapsed = (perf_counter() - started) * 1000
        return {"total_ms": elapsed, "per_result_ms": elapsed / len(results)}
    finally:
        await database.close()


def mode_stats(samples: list[dict], mode: str) -> dict:
    batch = [sample["batch_ms"] for sample in samples if sample["mode"] == mode]
    first = [
        sample["first_ms"]
        for sample in samples
        if sample["mode"] == mode and sample["first_ms"] is not None
    ]
    return {"batch": summarize(batch), "first": summarize(first)}


def run_benchmark(experiment_dir: str, *, tmp_root: Path | None = None) -> BenchmarkResult:
    resolved_tmp = None if tmp_root is None else Path(tmp_root)
    directory, root, prefix, paths = build_files(resolved_tmp)
    try:
        file_bytes = paths[0].stat().st_size
        read = create_file_tools(TEST_SESSION, tmp_root=root)["read"]
        tools = {"read": read}
        declared = {"read": read.definition()}
        calls = build_calls(prefix, paths)

        async def measure() -> list[dict]:
            for mode in ("sequential", "parallel"):
                for _ in range(WARMUP):
                    await run_batch(mode, calls, tools, declared)
            samples = []
            for pair in range(PAIRS):
                order = (
                    ("sequential", "parallel")
                    if pair % 2 == 0
                    else ("parallel", "sequential")
                )
                for position, mode in enumerate(order):
                    result, finalized = await run_batch(mode, calls, tools, declared)
                    samples.append(sample_of(mode, pair, position, result, finalized))
            return samples

        samples = asyncio.run(measure())
        integration, parallel_results = loop_integration(read, calls)
        persistence = asyncio.run(persist(parallel_results, calls, read, resolved_tmp))
    finally:
        directory.cleanup()

    serial = mode_stats(samples, "sequential")
    parallel = mode_stats(samples, "parallel")
    measured = sum(sample["result_count"] for sample in samples)
    correct = sum(sample["correct"] for sample in samples)
    errors = sum(sample["errors"] for sample in samples)
    state = git_state()

    env = {
        "scope": SCOPE,
        "commit": state["commit"],
        "worktree_status": state["status_porcelain"],
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "workload": {
            "files": FILES,
            "lines_per_file": LINES,
            "file_bytes": file_bytes,
            "tool": "read",
            "read_limit": LINES,
        },
        "cache_conditions": (
            f"文件在同一临时目录一次性写入；每种模式预热 {WARMUP} 次后测量，"
            "测量期间文件命中操作系统页缓存"
        ),
        "warmup": WARMUP,
        "pairs": PAIRS,
        "statistics": "P50 与探索性 P95 由 statistics.quantiles(n=100, method='inclusive') 取值",
    }
    stats = {
        "n_pairs": PAIRS,
        "n_samples": len(samples),
        "serial": serial,
        "parallel": parallel,
        "serial_over_parallel": {
            "batch_p50": serial["batch"]["p50"] / parallel["batch"]["p50"],
            "first_p50": serial["first"]["p50"] / parallel["first"]["p50"],
        },
        "exploratory_p95_note": f"P95 由 {PAIRS} 个配对样本估计，尾部稳定性有限",
        "persistence_ms": persistence,
    }
    correctness = {
        "batches": 2 * PAIRS,
        "expected_results_per_batch": FILES,
        "denominator": 2 * PAIRS * FILES,
        "measured_results": measured,
        "correct_results": correct,
        "error_results": errors,
        "loop_integration": integration,
    }
    assert measured == 2 * PAIRS * FILES and correct == measured and errors == 0
    assert integration["sequential"]["correct"] == FILES
    assert integration["parallel"]["correct"] == FILES

    result = BenchmarkResult(
        topic="tool_parallel",
        env=env,
        samples=samples,
        stats=stats,
        correctness=correctness,
        missing=["前端接收与传输耗时（本专项只测量后端工具调度，未测量前端）"],
        review=[
            {
                "review_id": "p95_stability",
                "standard": "P95 为探索性指标，30 个样本对尾部估计有限，结论以批次 P50 为主",
                "evidence_refs": ["stats.serial.batch", "stats.parallel.batch"],
            }
        ],
        status="complete",
    )
    target = Path(experiment_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "tool_parallel.json").write_text(
        json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def check() -> None:
    result = run_benchmark(str(ROOT))
    environment = result.env
    serial = result.stats["serial"]
    parallel = result.stats["parallel"]
    print(
        f"环境: {environment['platform']} / Python {environment['python']}；"
        f"commit {environment['commit'][:12]}；"
        f"{environment['workload']['files']} 个 {environment['workload']['lines_per_file']} 行文件；"
        f"预热 {environment['warmup']} 次，{environment['pairs']} 组配对"
    )
    for label, key in (("批次耗时", "batch"), ("首结果耗时", "first")):
        serial_stats = serial[key]
        parallel_stats = parallel[key]
        print(
            f"{label}(ms): 串行 P50={serial_stats['p50']:.2f} P95={serial_stats['p95']:.2f} | "
            f"并行 P50={parallel_stats['p50']:.2f} P95={parallel_stats['p95']:.2f} | "
            f"串/并={serial_stats['p50'] / parallel_stats['p50']:.2f}x"
        )
    correctness = result.correctness
    print(
        f"正确性: {correctness['correct_results']}/{correctness['denominator']} 结果正确，"
        f"错误 {correctness['error_results']}"
    )
    persistence = result.stats["persistence_ms"]
    print(
        f"持久化(ms): {correctness['expected_results_per_batch']} 条结果入库 "
        f"总计={persistence['total_ms']:.2f}，每条={persistence['per_result_ms']:.3f}"
    )
    print(f"证据: {ROOT / 'tool_parallel.json'}")
    print(f"PASS: {environment['scope']} 的串行/并行真实样本与正确性校验完成")


if __name__ == "__main__":
    check()
