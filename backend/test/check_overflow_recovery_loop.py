import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from openai._streaming import SSEDecoder
from pydantic import TypeAdapter

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools.business import business_tool_declarations
from app.agent.tools.files import create_file_tools
from app.ai.messages import SystemMessage, UserMessage
from app.ai.model_capabilities import resolve_model_spec
from app.ai.overflow import is_context_overflow_error
from app.ai.stream import stream
from app.domain.session.models import CompactionEntry, Session, SessionMessageEntry
from app.infrastructure.persistence.sqlite import database as database_module
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces import http as http_module
from app.interfaces.http import CONTEXT_OVERFLOW_MESSAGE, active, app
from app.model_config import load_model_config
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
)

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-overflow-recovery"
TEMP_ROOT = ROOT / "data"

# 临时测试能力覆盖：使 Loop 不预压缩，真实超容量请求直接到达供应商触发容量错误。
# 生产 context_window（128000）与 reserve/keep 保持已确认值不变。
LARGE_CONTEXT_WINDOW = 4_000_000
PROBE_TOKENS = (300000, 200000, 150000, 120000, 100000, 80000, 60000)


def uuid() -> str:
    return str(uuid4())


def filler(tokens: int) -> str:
    # 每个 "word " 约一个真实 token，便于按 token 数构造输入。
    return "word " * tokens


def system_message(session_id: str) -> SystemMessage:
    tools = create_file_tools(session_id)
    return SystemMessage(
        role="system",
        content=SYSTEM_PROMPT,
        tools_added=[tool.definition() for tool in tools.values()] + business_tool_declarations(),
        timestamp=time.time_ns() // 1_000_000,
    )


def message_entry(session_id: str, entry_id: str, parent_id: str | None, message, created_at: int):
    return SessionMessageEntry(
        session_id=session_id, id=entry_id, parent_id=parent_id, run_id=None,
        type="message", messages=[message], created_at=created_at,
    )


async def _consume(spec, context, options):
    response = stream(spec, context, options)
    async for _ in response:
        pass
    return await response.result()


def probe_request(spec, api_key: str, tokens: int):
    # 直接请求供应商；容量错误被适配器转换为 error 助手消息，用于探测真实边界。
    context = {"messages": [UserMessage(role="user", content=filler(tokens), timestamp=1)]}
    options = {"api_key": api_key, "max_tokens": 64}
    try:
        return asyncio.run(_consume(spec, context, options))
    except BaseException as error:
        return error


def probe_capacity(spec, api_key: str) -> dict:
    accepted_max: int | None = None
    rejected_min: int | None = None
    for tokens in PROBE_TOKENS:
        outcome = probe_request(spec, api_key, tokens)
        if isinstance(outcome, BaseException):
            if is_context_overflow_error(outcome):
                rejected_min = tokens
                continue
            raise AssertionError(f"非容量错误: {type(outcome).__name__}: {outcome}")
        if outcome.stop_reason == "error" and outcome.context_overflow:
            rejected_min = tokens
            continue
        accepted_max = tokens
        break
    return {"accepted_max": accepted_max, "rejected_min": rejected_min}


async def seed(path: Path, session_id: str, huge: int, retained: int) -> dict:
    database = await database_module.open_database(path)
    repository = SqliteSessionRepository(database)
    ids: dict[str, str] = {"session": session_id}
    try:
        async with repository.transaction():
            await repository.insert_session(
                Session(id=session_id, title="溢出恢复", active_leaf_id=None, created_at=1, updated_at=1)
            )
            system, user1, user2 = uuid(), uuid(), uuid()
            ids.update(system=system, user1=user1, user2=user2)
            await repository.insert_entry(
                message_entry(session_id, system, None, system_message(session_id), 10)
            )
            await repository.insert_entry(
                message_entry(
                    session_id, user1, system,
                    UserMessage(role="user", content=filler(huge), timestamp=11), 11,
                )
            )
            await repository.insert_entry(
                message_entry(
                    session_id, user2, user1,
                    UserMessage(role="user", content=filler(retained), timestamp=12), 12,
                )
            )
            session = await repository.get_session(session_id)
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": user2, "updated_at": 12})
            )
    finally:
        await database.close()
    return ids


def events(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    for event in SSEDecoder().iter_bytes(response.iter_bytes()):
        yield {"event": event.event, "data": TypeAdapter(dict).validate_json(event.data)}


def wait_idle() -> None:
    deadline = time.monotonic() + 180
    while active.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not active.locked(), "工作线程未释放运行占用"


def submit(http, session_id: str, text: str) -> tuple[str, list[dict]]:
    payload = {
        "session_id": session_id, "operation_id": uuid(), "request": text, "attachments": [],
    }
    with http.stream("POST", "/api/agent/run", json=payload) as response:
        request_entry_id = response.headers["X-Request-Entry-ID"]
        result = list(events(response))
    wait_idle()
    return request_entry_id, result


def call(coro, loop):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def success_scenario(http, ids: dict, trigger: str) -> dict:
    session_id = ids["session"]
    service = app.state.session_service
    loop = app.state.loop
    request_entry_id, result = submit(http, session_id, trigger)
    kinds = [event["event"] for event in result]

    starts = [event for event in result if event["event"] == "compaction_start"]
    ends = [event for event in result if event["event"] == "compaction_end"]
    assert len(starts) == 1 and starts[0]["data"]["reason"] == "overflow", kinds
    assert len(ends) == 1, kinds
    assert ends[0]["data"]["entry_id"] == starts[0]["data"]["compaction_id"]
    assert ends[0]["data"]["parent_id"]

    failed_ends = [
        event for event in result
        if event["event"] == "message_end" and event["data"]["stop_reason"] == "error"
    ]
    assert len(failed_ends) == 1, kinds
    assert kinds[-1] == "done", result[-1]
    assert kinds.index("compaction_start") < kinds.index("compaction_end")

    entries = call(service.list_entries(session_id), loop)
    failed_entry = next(
        entry for entry in entries
        if isinstance(entry, SessionMessageEntry)
        and entry.messages[0].role == "assistant"
        and entry.messages[0].stop_reason == "error"
    )
    assert failed_entry.model_omitted is True
    assert failed_entry.messages[0].context_overflow is True
    assert failed_entry.messages[0].usage.total_tokens == 0
    assert str(failed_entry.id) == failed_ends[0]["data"]["entry_id"]

    checkpoints = [entry for entry in entries if isinstance(entry, CompactionEntry)]
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint.id == starts[0]["data"]["compaction_id"]
    assert checkpoint.parent_id == failed_entry.id
    assert str(checkpoint.first_kept_entry_id) == ids["user2"]
    assert checkpoint.usage.total_tokens > 0

    run = call(service.list_runs(session_id), loop)[-1]
    assert run.status == "completed", (run.status, run.error_code, run.error_message)
    assert str(run.request_entry_id) == request_entry_id
    assert run.id == result[0]["data"]["run_id"]

    projection = call(service.get_projection(session_id, run.last_entry_id), loop)
    assert str(failed_entry.id) not in [str(item) for item in projection.context.source_entry_ids]
    return {
        "run_id": run.id,
        "request_entry_id": request_entry_id,
        "checkpoint_id": checkpoint.id,
        "failed_entry_id": failed_entry.id,
        "summary_usage": checkpoint.usage.model_dump(),
        "tokens_before": checkpoint.tokens_before,
        "events": kinds,
    }


def second_overflow_scenario(http, ids: dict, trigger: str) -> dict:
    session_id = ids["session"]
    service = app.state.session_service
    loop = app.state.loop
    request_entry_id, result = submit(http, session_id, trigger)
    kinds = [event["event"] for event in result]

    starts = [event for event in result if event["event"] == "compaction_start"]
    ends = [event for event in result if event["event"] == "compaction_end"]
    assert len(starts) == 1 and starts[0]["data"]["reason"] == "overflow", kinds
    assert len(ends) == 1 and ends[0]["data"]["entry_id"] == starts[0]["data"]["compaction_id"], kinds

    failed_ends = [
        event for event in result
        if event["event"] == "message_end" and event["data"]["stop_reason"] == "error"
    ]
    assert len(failed_ends) == 2, kinds
    final = result[-1]
    assert final["event"] == "error", final
    assert final["data"]["status"] == "failed"
    assert final["data"]["code"] == "context_overflow"
    assert final["data"]["message"] == CONTEXT_OVERFLOW_MESSAGE

    entries = call(service.list_entries(session_id), loop)
    omitted = [
        entry for entry in entries
        if isinstance(entry, SessionMessageEntry) and entry.model_omitted
    ]
    assert len(omitted) == 2
    assert all(entry.messages[0].context_overflow for entry in omitted)
    checkpoints = [entry for entry in entries if isinstance(entry, CompactionEntry)]
    assert len(checkpoints) == 1

    run = call(service.list_runs(session_id), loop)[-1]
    assert (run.status, run.error_code, run.error_message) == (
        "failed", "context_overflow", CONTEXT_OVERFLOW_MESSAGE,
    )
    assert str(run.request_entry_id) == request_entry_id
    return {
        "run_id": run.id,
        "error_code": run.error_code,
        "failed_entries": [entry.id for entry in omitted],
        "events": kinds,
    }


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    install_test_model_config()
    config = load_model_config()
    api_key = config.api_key
    spec = resolve_model_spec(config)

    evidence: dict = {"spec": {"id": spec.id, "context_window": spec.context_window, "max_tokens": spec.max_tokens}}
    probe = probe_capacity(spec, api_key)
    evidence["probe"] = probe
    if probe["rejected_min"] is None or probe["accepted_max"] is None:
        evidence["status"] = "blocked"
        evidence["reason"] = "供应商未返回容量错误，无法触发真实溢出"
        (ROOT / "loop-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("BLOCKED: 供应商未返回容量错误，真实恢复链未验证", probe)
        return

    accepted = probe["accepted_max"]
    rejected = probe["rejected_min"]
    huge = int(accepted * 0.8)
    retained_ok = int(accepted * 0.45)
    # 二次溢出：保留原文本身仍超出真实窗口，恢复续接再次溢出。
    retained_overflow = int(rejected * 1.1)
    evidence["sizes"] = {
        "huge": huge,
        "retained_ok": retained_ok,
        "retained_overflow": retained_overflow,
    }

    original_resolve = http_module.resolve_model_spec

    def patched_resolve(model_config):
        resolved = original_resolve(model_config)
        return resolved.model_copy(update={"context_window": LARGE_CONTEXT_WINDOW})

    http_module.resolve_model_spec = patched_resolve

    path = patch_default_database("overflow-recovery")
    success_session = uuid()
    ids_success = asyncio.run(seed(path, success_session, huge, retained_ok))
    overflow_session = uuid()
    ids_overflow = asyncio.run(seed(path, overflow_session, huge, retained_overflow))
    evidence["sessions"] = {"success": success_session, "overflow": overflow_session}
    trigger = "基于以上历史继续，请只用一句话确认你能看到最近的保留原文。"
    with Server(app) as server, client(server.base_url) as http:
        for session_id in (success_session, overflow_session):
            created = http.post(
                "/api/sessions", json={"session_id": session_id, "title": "溢出恢复"}
            )
            assert created.status_code in {200, 201}, created.text
        evidence["recovery"] = success_scenario(http, ids_success, trigger)
        evidence["second_overflow"] = second_overflow_scenario(http, ids_overflow, trigger)
    assert not active.locked()
    evidence["status"] = "verified"
    (ROOT / "loop-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS: 真实一次溢出→一次摘要→一次续接恢复链、二次溢出 context_overflow 收尾通过")


if __name__ == "__main__":
    check()
