import asyncio
import platform
import sys
from pathlib import Path
from statistics import quantiles
from tempfile import TemporaryDirectory
from time import perf_counter, time_ns
from types import SimpleNamespace
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
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
from test.regression_support import TEST_SESSION, session_workspace, temporary_root

FILES = 64
LINES = 800
REPS = 20
WARMUP = 2
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


def build_files() -> tuple[TemporaryDirectory, Path, str, list[Path]]:
    # 文件工具按统一 tmp 根解析相对路径，测量文件位于当前会话 workspace 之下。
    directory = TemporaryDirectory()
    root = Path(directory.name).resolve()
    workspace, prefix = session_workspace(root)
    payload = "".join(f"line-{index:05d} " + "x" * 69 + "\n" for index in range(LINES))
    paths = []
    for index in range(FILES):
        path = workspace / f"f{index:02d}.txt"
        path.write_text(payload, encoding="utf-8")
        paths.append(path)
    return directory, root, prefix, paths


async def noop(_context, _signal):
    return None


async def run_once(mode, system, tools, tool_use, stop):
    events = []
    count = {"value": 0}

    async def emit(event):
        events.append(event)

    def stream_fn(_model_spec, _context, _options):
        count["value"] += 1
        return single(tool_use if count["value"] == 1 else stop)

    config = AgentLoopConfig(
        model=MODEL, max_turns=4, tool_execution=mode, after_tool_call=noop
    )
    messages = await run_agent_loop(
        [UserMessage(role="user", content="读取全部文件", timestamp=0)],
        {"messages": [system], "tools": tools},
        config,
        emit,
        stream_fn=stream_fn,
    )
    # 批次与单工具耗时只取真实 loop 事件：turn_end 的批次时间、tool_execution_end 的执行与后处理耗时。
    turn = next(
        event
        for event in events
        if event["type"] == "turn_end" and event["tool_first_result_at"] is not None
    )
    finals = [event for event in events if event["type"] == "tool_execution_end"]
    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    assert len(results) == FILES and all(not message.is_error for message in results)
    assert len(finals) == FILES
    return {
        "batch_ms": (turn["tool_finalized_at"] - turn["tool_prepare_started_at"]) * 1000,
        "first_ms": (turn["tool_first_result_at"] - turn["tool_prepare_started_at"]) * 1000,
        "exec_ms": [event["duration_ms"] for event in finals],
        "post_ms": [event["postprocess_ms"] for event in finals if event["postprocess_ms"] is not None],
    }, results


def check_parallel() -> dict:
    directory, root, prefix, paths = build_files()
    try:
        read = create_file_tools(TEST_SESSION, tmp_root=root)["read"]
        calls = [
            ToolCall(
                type="toolCall",
                id=f"c{index:02d}",
                name="read",
                arguments={"path": f"{prefix}/{path.name}"},
            )
            for index, path in enumerate(paths)
        ]
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

        async def measure(mode: str):
            batch_ms: list[float] = []
            first_ms: list[float] = []
            exec_ms: list[float] = []
            post_ms: list[float] = []
            results: list[ToolResultMessage] = []
            for index in range(WARMUP + REPS):
                outcome, results = await run_once(mode, system, tools, tool_use, stop)
                if index < WARMUP:
                    continue
                batch_ms.append(outcome["batch_ms"])
                first_ms.append(outcome["first_ms"])
                exec_ms += outcome["exec_ms"]
                post_ms += outcome["post_ms"]
            return (
                {
                    "batch": summarize(batch_ms),
                    "first": summarize(first_ms),
                    "exec": summarize(exec_ms),
                    "post": summarize(post_ms),
                },
                results,
            )

        serial, _ = asyncio.run(measure("sequential"))
        parallel, results = asyncio.run(measure("parallel"))
        persistence = asyncio.run(persist(results, calls, read))
        return {
            "environment": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "files": FILES,
                "file_kb": round(paths[0].stat().st_size / 1024, 1),
                "warmup": WARMUP,
                "reps": REPS,
            },
            "serial": serial,
            "parallel": parallel,
            "persistence_ms": persistence,
        }
    finally:
        directory.cleanup()


async def persist(
    results: list[ToolResultMessage], calls: list[ToolCall], read
) -> dict:
    database = await open_database(
        temporary_root("tool-parallel-perf") / f"persist-{uuid4().hex}.db"
    )
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
        await service.append_entry(
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
            outcome = await service.append_entry(
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


def check() -> None:
    evidence = check_parallel()
    environment = evidence["environment"]
    serial = evidence["serial"]
    parallel = evidence["parallel"]
    print(
        f"环境: {environment['platform']} / Python {environment['python']}；"
        f"{environment['files']} 个约 {environment['file_kb']}KB 文件；"
        f"预热 {environment['warmup']} 次，测量 {environment['reps']} 次"
    )
    for label, key in (("批次耗时", "batch"), ("首结果耗时", "first")):
        serial_stats = serial[key]
        parallel_stats = parallel[key]
        print(
            f"{label}(ms): 串行 P50={serial_stats['p50']:.2f} P95={serial_stats['p95']:.2f} | "
            f"并行 P50={parallel_stats['p50']:.2f} P95={parallel_stats['p95']:.2f} | "
            f"串/并={serial_stats['p50'] / parallel_stats['p50']:.2f}x"
        )
    for label, key in (("单工具执行", "exec"), ("单工具后处理", "post")):
        print(
            f"{label}(ms): n={serial[key]['n']} "
            f"串行 P50={serial[key]['p50']:.3f} P95={serial[key]['p95']:.3f} | "
            f"并行 P50={parallel[key]['p50']:.3f} P95={parallel[key]['p95']:.3f}"
        )
    persistence = evidence["persistence_ms"]
    print(
        f"持久化(ms): 64 条结果入库 总计={persistence['total_ms']:.2f}，"
        f"每条={persistence['per_result_ms']:.3f}"
    )
    print("前端接收与传输耗时: 未测量（按本次约定不实测）")
    print("PASS: 真实 loop 事件（turn_end / tool_execution_end）串行/并行测量完成")


if __name__ == "__main__":
    check()
