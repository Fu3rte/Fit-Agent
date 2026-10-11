import asyncio
import json
from pathlib import Path

from app.agent.compaction import estimate_context_tokens
from app.ai.messages import AssistantMessage, ToolResultMessage, UserMessage
from app.ai.model_capabilities import resolve_model_spec
from app.application.session.service import SessionService
from app.domain.session.models import SessionMessageEntry
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces import http as http_module
from app.interfaces.http import active, app
from app.model_config import load_model_config
from test.check_overflow_recovery_loop import (
    LARGE_CONTEXT_WINDOW,
    ROOT,
    filler,
    probe_capacity,
    submit,
    uuid,
)
from test.check_profile_confirmation import (
    Fixture,
    count_rows,
    payload,
    prepare_arguments,
    propose,
)
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
)

PREPARE = "prepare_profile_update"


def message_entry(session_id: str, entry_id: str, parent_id: str | None, message, created_at: int):
    return SessionMessageEntry(
        session_id=session_id, id=entry_id, parent_id=parent_id, run_id=None,
        type="message", messages=[message], created_at=created_at,
    )


async def seed_tool_chain(path: Path, huge: int, retained: int) -> dict:
    # 真实工具 + 真实业务保存：准备工具执行一次并提交结果节点与业务版本。
    fixture = await Fixture(path).seeded()
    try:
        session_id = await fixture.session()
        chain = await propose(fixture, session_id, prepare_arguments(None, payload()))
        save = await fixture.service_save(chain)
        assert save.version == 1
        before = await fixture.profile_state()
        assert before == {"version": 1, "profile_rows": 1, "record_rows": 1}, before

        repository = SqliteSessionRepository(fixture.database)
        huge_id, retained_id = uuid(), uuid()
        async with repository.transaction():
            await repository.insert_entry(message_entry(
                session_id, huge_id, chain["confirmation"],
                UserMessage(role="user", content=filler(huge), timestamp=20), 20,
            ))
            await repository.insert_entry(message_entry(
                session_id, retained_id, huge_id,
                UserMessage(role="user", content=filler(retained), timestamp=21), 21,
            ))
            session = await repository.get_session(session_id)
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": retained_id, "updated_at": 21})
            )
        return {"session": session_id, "huge": huge_id, "retained": retained_id,
                "before": before, **chain}
    finally:
        await fixture.close()


async def inspect(path: Path, ids: dict) -> dict:
    # 恢复后核查：工具调用身份、结果父子链与业务写入保持一次，未重复执行。
    database = await open_database(path)
    try:
        repository = SqliteSessionRepository(database)
        entries = await repository.list_entries(ids["session"])
        by_id = {entry.id: entry for entry in entries}
        display = by_id[ids["display"]]
        result = display.messages[0]
        assert isinstance(result, ToolResultMessage), result
        assert result.tool_name == PREPARE and result.tool_call_id == ids["call"]
        assert result.is_error is False
        assert display.parent_id == ids["source"]
        source = by_id[ids["source"]]
        assert isinstance(source.messages[0], AssistantMessage)
        assert source.messages[0].stop_reason == "toolUse"
        assert source.messages[0].content[0].id == ids["call"]
        business = SqliteBusinessRepository(database)
        snapshot = await business.get_snapshot(ids["proposal"])
        assert snapshot is not None and snapshot.display_entry_id == ids["display"]
        return {
            "status": getattr(snapshot.status, "value", snapshot.status),
            "profile_rows": await count_rows(database, "profile"),
            "record_rows": await count_rows(database, "profile_save_records"),
        }
    finally:
        await database.close()


async def diagnose(path: Path, ids: dict) -> dict:
    database = await open_database(path)
    try:
        service = SessionService(SqliteSessionRepository(database))
        projection = await service.get_projection(ids["session"], ids["retained"])
        return {
            "messages": len(projection.context.messages),
            "sources": len(projection.context.source_entry_ids),
            "tokens": estimate_context_tokens(projection.context.messages).tokens,
        }
    finally:
        await database.close()


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    install_test_model_config()
    config = load_model_config()
    api_key = config.api_key
    spec = resolve_model_spec(config)

    evidence: dict = {"spec": {"id": spec.id, "context_window": spec.context_window}}
    probe = probe_capacity(spec, api_key)
    evidence["probe"] = probe
    if probe["rejected_min"] is None or probe["accepted_max"] is None:
        evidence["status"] = "blocked"
        evidence["reason"] = "供应商未返回容量错误，工具副作用恢复链未验证"
        _write(evidence)
        print("BLOCKED: 供应商未返回容量错误，工具副作用恢复链未验证", probe)
        return

    accepted = probe["accepted_max"]
    huge = int(accepted * 0.8)
    retained = int(accepted * 0.75)
    evidence["sizes"] = {"huge": huge, "retained": retained}

    original_resolve = http_module.resolve_model_spec

    def patched_resolve(model_config):
        resolved = original_resolve(model_config)
        return resolved.model_copy(update={"context_window": LARGE_CONTEXT_WINDOW})

    http_module.resolve_model_spec = patched_resolve

    path = patch_default_database("overflow-tool-side-effect")
    ids = asyncio.run(seed_tool_chain(path, huge, retained))
    evidence["session"] = ids["session"]

    evidence["projection"] = asyncio.run(diagnose(path, ids))
    trigger = "基于以上历史继续，请只用一句话确认你能看到最近的保留原文。"
    with Server(app) as server, client(server.base_url) as http:
        created = http.post(
            "/api/sessions", json={"session_id": ids["session"], "title": "画像确认会话"}
        )
        assert created.status_code in {200, 201}, created.text
        request_entry_id, result = submit(http, ids["session"], trigger)
    assert not active.locked()

    kinds = [event["event"] for event in result]
    starts = [event for event in result if event["event"] == "compaction_start"]
    ends = [event for event in result if event["event"] == "compaction_end"]
    assert len(starts) == 1 and starts[0]["data"]["reason"] == "overflow", kinds
    assert len(ends) == 1 and ends[0]["data"]["entry_id"] == starts[0]["data"]["compaction_id"]
    failed_ends = [
        event for event in result
        if event["event"] == "message_end" and event["data"]["stop_reason"] == "error"
    ]
    assert len(failed_ends) == 1, kinds
    assert kinds[-1] == "done", result[-1]

    state = asyncio.run(inspect(path, ids))
    # 工具调用身份、结果父子链、业务版本与保存记录保持一次，未因恢复重复执行。
    assert state == {"status": "saved", "profile_rows": 1, "record_rows": 1}, state
    assert state["profile_rows"] == ids["before"]["profile_rows"]
    assert state["record_rows"] == ids["before"]["record_rows"]

    evidence["recovery"] = {
        "request_entry_id": request_entry_id,
        "events": kinds,
        "failed_entry_id": failed_ends[0]["data"]["entry_id"],
    }
    evidence["tool_side_effect"] = {
        "tool_call_id": ids["call"],
        "display_entry_id": ids["display"],
        "parent_entry_id": ids["source"],
        "business": state,
    }
    evidence["status"] = "verified"
    _write(evidence)
    print("PASS: 真实工具执行后溢出恢复，工具与业务副作用保持一次通过")


def _write(evidence: dict) -> None:
    (ROOT / "tool-side-effect-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    check()
