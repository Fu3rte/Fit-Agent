import asyncio
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from openai._streaming import SSEDecoder
from pydantic import TypeAdapter

from app.agent.compaction import (
    KEEP_RECENT_TOKENS,
    RESERVE_TOKENS,
    CompactionSummaryError,
    ContextBudgetExceeded,
    NoSummarizableHistory,
    estimate_context_tokens,
    should_compact,
)
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools.business import business_tool_declarations
from app.agent.tools.files import create_file_tools
from app.agent.usage import summarize_usage
from app.ai.messages import AssistantMessage, SystemMessage, Usage, UserMessage
from app.domain.session.models import CompactionEntry, Session, SessionMessageEntry
from app.infrastructure.persistence.sqlite import database as database_module
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import (
    COMPACTION_FAILED_MESSAGE,
    CONTEXT_BUDGET_EXCEEDED_MESSAGE,
    active,
    app,
    terminal_decision,
)
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
)

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-loop-part-2"
TEMP_ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-loop-part-2-data"

CONTEXT_WINDOW = 128000
KEEP = 1000
# 待压缩历史：一条超长中文用户消息（真实中文 token 远多于 chars/4 估算）。
LONG_USER = "计划调整历史上下文占位。" * 4700
# 近期保留原文的估算长度必须不小于保留预算，切点才会落在它之上、留下可总结前缀。
RETAINED_USER = "近期保留的原文占位。" * 700
TRIGGER_REQUEST = "基于以上历史继续，请只用一句话确认你能看到最近的保留原文。" * 4


def uuid() -> str:
    return str(uuid4())


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


async def seed(path: Path, session_id: str) -> dict:
    database = await database_module.open_database(path)
    repository = SqliteSessionRepository(database)
    ids: dict[str, str] = {"session": session_id}
    try:
        async with repository.transaction():
            await repository.insert_session(
                Session(id=session_id, title="压缩调度", active_leaf_id=None, created_at=1, updated_at=1)
            )
            system, user1, user2 = uuid(), uuid(), uuid()
            ids.update(system=system, user1=user1, user2=user2)
            await repository.insert_entry(
                message_entry(session_id, system, None, system_message(session_id), 10)
            )
            await repository.insert_entry(
                message_entry(
                    session_id, user1, system,
                    UserMessage(role="user", content=LONG_USER, timestamp=11), 11,
                )
            )
            await repository.insert_entry(
                message_entry(
                    session_id, user2, user1,
                    UserMessage(role="user", content=RETAINED_USER, timestamp=12), 12,
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
    deadline = time.monotonic() + 90
    while active.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not active.locked(), "工作线程未释放运行占用"


def submit(http, session_id: str, text: str) -> list[dict]:
    payload = {
        "session_id": session_id, "operation_id": uuid(), "request": text, "attachments": [],
    }
    with http.stream("POST", "/api/agent/run", json=payload) as response:
        result = list(events(response))
    wait_idle()
    return result


def call(coro, loop):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def pure_checks() -> dict:
    # 生产预算与阈值：context_tokens > context_window - reserve 触发。
    assert RESERVE_TOKENS == 16384 and KEEP_RECENT_TOKENS == 20000
    assert should_compact(CONTEXT_WINDOW - RESERVE_TOKENS + 1, CONTEXT_WINDOW, RESERVE_TOKENS) is True
    assert should_compact(CONTEXT_WINDOW - RESERVE_TOKENS, CONTEXT_WINDOW, RESERVE_TOKENS) is False
    # 固定失败码与文案：SSE 与运行记录使用相同文本。
    assert terminal_decision(CompactionSummaryError("x"), False) == (
        "failed", "compaction_failed", COMPACTION_FAILED_MESSAGE,
    )
    assert terminal_decision(ContextBudgetExceeded("x"), False) == (
        "failed", "context_budget_exceeded", CONTEXT_BUDGET_EXCEEDED_MESSAGE,
    )
    assert terminal_decision(NoSummarizableHistory("x"), False) == (
        "failed", "context_budget_exceeded", CONTEXT_BUDGET_EXCEEDED_MESSAGE,
    )
    assert terminal_decision(None, True) == ("cancelled", "cancelled", "执行已取消。")
    # 摘要用量只计入一次，且真实用量进入总统计。
    assistant = AssistantMessage(
        role="assistant", content=[], api="openai-completions", provider="p", model="m",
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason="stop", timestamp=1,
    )
    summary_usage = Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=7)
    totals = summarize_usage([assistant], compaction_usage=[summary_usage])
    assert totals.total_tokens == 9
    return {"threshold": CONTEXT_WINDOW - RESERVE_TOKENS}


def compaction_checks(http, ids: dict) -> dict:
    session_id = ids["session"]
    service = app.state.session_service
    loop = app.state.loop
    projection = call(service.get_projection(session_id, ids["user2"]), loop)
    seeded = estimate_context_tokens(projection.context.messages).tokens
    os.environ["FIT_AGENT_COMPACTION_RESERVE_TOKENS"] = str(CONTEXT_WINDOW - seeded)
    os.environ["FIT_AGENT_COMPACTION_KEEP_RECENT_TOKENS"] = str(KEEP)

    result = submit(http, session_id, TRIGGER_REQUEST)
    kinds = [event["event"] for event in result]
    assert kinds[-1] == "done", result[-1]
    assert kinds.count("compaction_start") == 1 and kinds.count("compaction_end") == 1, kinds
    start = next(event for event in result if event["event"] == "compaction_start")
    end = next(event for event in result if event["event"] == "compaction_end")
    assert start["data"]["reason"] == "threshold"
    assert end["data"]["entry_id"] == start["data"]["compaction_id"]
    assert end["data"]["parent_id"]
    assert kinds.index("compaction_start") < kinds.index("compaction_end") < kinds.index("message_start")

    entries = call(service.list_entries(session_id), loop)
    compactions = [entry for entry in entries if isinstance(entry, CompactionEntry)]
    assert len(compactions) == 1
    checkpoint = compactions[0]
    assert checkpoint.id == end["data"]["entry_id"]
    assert checkpoint.parent_id == end["data"]["parent_id"]
    assert checkpoint.first_kept_entry_id == ids["user2"]
    assert checkpoint.summary.strip() and checkpoint.tokens_before > 0
    assert checkpoint.usage.total_tokens > 0
    assert checkpoint.system_message.content == SYSTEM_PROMPT
    run = call(service.list_runs(session_id), loop)[-1]
    assert run.status == "completed" and run.request_entry_id != ids["user2"]
    assert run.id == result[0]["data"]["run_id"]

    history = http.get(f"/api/sessions/{session_id}/history").json()
    types = [entry["type"] for entry in history["entries"]]
    assert types.count("compaction") == 1
    assert history["session"]["active_leaf_id"] == history["entries"][-1]["entry_id"]

    committed = call(service.get_current_branch(session_id), loop)
    messages = [entry.messages[0] for entry in committed if isinstance(entry, SessionMessageEntry)]
    totals = summarize_usage(messages, compaction_usage=[checkpoint.usage])
    assert totals.total_tokens >= checkpoint.usage.total_tokens
    return {
        "seeded_tokens": seeded,
        "checkpoint_id": checkpoint.id,
        "first_kept_entry_id": checkpoint.first_kept_entry_id,
        "tokens_before": checkpoint.tokens_before,
        "summary_usage": checkpoint.usage.model_dump(),
        "run_id": run.id,
    }


def no_trigger_checks(http, session_id: str) -> dict:
    os.environ["FIT_AGENT_COMPACTION_RESERVE_TOKENS"] = "1024"
    os.environ["FIT_AGENT_COMPACTION_KEEP_RECENT_TOKENS"] = str(KEEP)
    result = submit(http, session_id, "只回复完成。")
    kinds = [event["event"] for event in result]
    assert kinds[-1] == "done", result[-1]
    assert "compaction_start" not in kinds and "compaction_end" not in kinds
    return {"events": kinds[-1]}


def budget_exceeded_checks(http, session_id: str) -> dict:
    service = app.state.session_service
    loop = app.state.loop
    os.environ["FIT_AGENT_COMPACTION_RESERVE_TOKENS"] = "127000"
    os.environ["FIT_AGENT_COMPACTION_KEEP_RECENT_TOKENS"] = "500"
    before = [
        entry for entry in call(service.list_entries(session_id), loop)
        if isinstance(entry, CompactionEntry)
    ]
    result = submit(http, session_id, "只回复完成。")
    kinds = [event["event"] for event in result]
    assert kinds.count("compaction_start") == 1 and "compaction_end" not in kinds, kinds
    final = result[-1]
    assert final["event"] == "error", final
    assert final["data"]["status"] == "failed"
    assert final["data"]["code"] == "context_budget_exceeded"
    assert final["data"]["message"] == CONTEXT_BUDGET_EXCEEDED_MESSAGE
    run = call(service.list_runs(session_id), loop)[-1]
    assert (run.status, run.error_code, run.error_message) == (
        "failed", "context_budget_exceeded", CONTEXT_BUDGET_EXCEEDED_MESSAGE,
    )
    after = [
        entry for entry in call(service.list_entries(session_id), loop)
        if isinstance(entry, CompactionEntry)
    ]
    assert after == before
    return {"code": final["data"]["code"], "message": final["data"]["message"]}


def pending_reference_checks(session_id: str) -> dict:
    business = app.state.business
    loop = app.state.loop
    references = call(business.list_pending_references(session_id), loop)
    assert references == []
    return {"pending_references": len(references)}


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ.pop("FIT_AGENT_COMPACTION_RESERVE_TOKENS", None)
    os.environ.pop("FIT_AGENT_COMPACTION_KEEP_RECENT_TOKENS", None)
    pure = pure_checks()
    install_test_model_config()
    path = patch_default_database("compaction-loop")
    seeded_session = uuid()
    ids = asyncio.run(seed(path, seeded_session))
    evidence: dict = {"pure": pure, "session": seeded_session}
    with Server(app) as server, client(server.base_url) as http:
        created = http.post(
            "/api/sessions", json={"session_id": seeded_session, "title": "压缩调度"}
        )
        assert created.status_code in {200, 201}, created.text
        evidence["pending"] = pending_reference_checks(seeded_session)
        evidence["compaction"] = compaction_checks(http, ids)

        plain_session = uuid()
        created = http.post(
            "/api/sessions", json={"session_id": plain_session, "title": "未触发"}
        )
        assert created.status_code in {200, 201}, created.text
        evidence["no_trigger"] = no_trigger_checks(http, plain_session)

        budget_session = uuid()
        created = http.post(
            "/api/sessions", json={"session_id": budget_session, "title": "预算不足"}
        )
        assert created.status_code in {200, 201}, created.text
        evidence["budget_exceeded"] = budget_exceeded_checks(http, budget_session)
    assert not active.locked()
    (ROOT / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: threshold 自动压缩（真实 start/end 事件、检查点身份一致、保留起点为近期原文、"
        "真实摘要用量与稳定系统状态、下一请求消费投影）、未触发路径、"
        "预算不足明确失败（固定 code/文案与真实运行记录）、只读待确认引用查询、"
        "生产阈值与失败码的固定输入校验"
    )


if __name__ == "__main__":
    check()
