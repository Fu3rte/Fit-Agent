import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from app import model_config
from app.agent.events import AgentEvent
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.application.session.service import CREDENTIAL_SAFE_MESSAGE
from app.domain.session.attachments import AttachmentMetadata, attachment_storage_ref
from app.domain.session.branch import build_session_path
from app.domain.session.models import (
    CompactionDetails,
    CompactionEntry,
    Session,
    SessionMessageEntry,
    SessionRun,
    SteeringInput,
)
from app.infrastructure.persistence.sqlite import database as database_module
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import (
    app,
    encode,
    history_entry,
    terminal_decision,
)
from test.regression_support import Server, client

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-public-contract"
TEMP_ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-public-contract-data"

USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
SYSTEM_MESSAGE = SystemMessage(
    role="system",
    content="系统提示词，仅后端可见。",
    tools_added=[],
    timestamp=100,
)
SUMMARY_TEXT = "压缩摘要正文不得公开"
CREDENTIAL_KEY = "sk-test-compaction-public-secret"

MESSAGE_KEYS = {"type", "entry_id", "parent_id", "run_id", "created_at", "message"}
COMPACTION_KEYS = {"type", "entry_id", "parent_id", "run_id", "created_at"}
COMPACTION_FORBIDDEN = (
    SUMMARY_TEXT,
    "首次保留",
    "tokens_before",
    "first_kept_entry_id",
    "system_message",
    "usage",
    "details",
    "系统提示词",
)


def nid() -> str:
    return str(uuid4())


def text(value: str) -> TextContent:
    return TextContent(type="text", text=value, text_signature="text-sig")


def assistant(content: list, stop_reason: str, timestamp: int) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="example",
        model="model-1",
        usage=USAGE,
        stop_reason=stop_reason,
        timestamp=timestamp,
    )


def message_entry(
    session_id: str,
    entry_id: str,
    parent_id: str | None,
    run_id: str | None,
    message,
    created_at: int,
) -> SessionMessageEntry:
    return SessionMessageEntry(
        session_id=session_id,
        id=entry_id,
        parent_id=parent_id,
        run_id=run_id,
        type="message",
        messages=[message],
        created_at=created_at,
    )


def compaction(
    session_id: str,
    entry_id: str,
    parent_id: str,
    run_id: str,
    first_kept: str,
    created_at: int,
    *,
    summary: str = SUMMARY_TEXT,
    details: CompactionDetails | None = None,
) -> CompactionEntry:
    return CompactionEntry(
        session_id=session_id,
        id=entry_id,
        parent_id=parent_id,
        run_id=run_id,
        type="compaction",
        summary=summary,
        first_kept_entry_id=first_kept,
        tokens_before=4096,
        usage=USAGE,
        system_message=SYSTEM_MESSAGE,
        details=details,
        created_at=created_at,
    )


async def seed(path: Path) -> dict:
    database = await database_module.open_database(path)
    repository = SqliteSessionRepository(database)
    ids: dict = {}
    try:
        sid = nid()
        ids["session"] = sid
        async with repository.transaction():
            await repository.insert_session(
                Session(id=sid, title="压缩历史", active_leaf_id=None, created_at=1, updated_at=1)
            )
            system_id, user1 = nid(), nid()
            ids["system"], ids["user1"] = system_id, user1
            await repository.insert_entry(
                message_entry(sid, system_id, None, None, SYSTEM_MESSAGE, 10)
            )
            await repository.insert_entry(
                message_entry(
                    sid, user1, system_id, None,
                    UserMessage(role="user", content="问题一", timestamp=11), 11,
                )
            )
            run_id = nid()
            ids["run"] = run_id
            await repository.insert_run(
                SessionRun(
                    session_id=sid, id=run_id, request_entry_id=user1, last_entry_id=None,
                    status="running", started_at=12, finished_at=None,
                    error_code=None, error_message=None,
                )
            )
            assistant1, tool1 = nid(), nid()
            ids["assistant1"], ids["tool1"] = assistant1, tool1
            await repository.insert_entry(
                message_entry(
                    sid, assistant1, user1, run_id,
                    assistant(
                        [
                            text("调用工具"),
                            ToolCall(type="toolCall", id="call-1", name="lookup", arguments={"q": 1}),
                        ],
                        "toolUse", 200,
                    ),
                    13,
                )
            )
            await repository.insert_entry(
                message_entry(
                    sid, tool1, assistant1, run_id,
                    ToolResultMessage(
                        role="toolResult", tool_call_id="call-1", tool_name="lookup",
                        content=[text("结果正文")], is_error=False, timestamp=220,
                    ),
                    14,
                )
            )
            attachment_id = nid()
            ids["attachment"] = attachment_id
            await repository.insert_attachment(
                AttachmentMetadata(
                    attachment_id=attachment_id, session_id=sid, file_name="note.md",
                    size_bytes=3,
                    storage_ref=attachment_storage_ref(sid, attachment_id, "note.md"),
                    created_at=15,
                )
            )
            await repository.bind_entry_attachments(sid, user1, [attachment_id])
            reference = {
                "attachment_id": attachment_id,
                "file_name": "note.md",
                "path": attachment_storage_ref(sid, attachment_id, "note.md").removeprefix("tmp/"),
            }
            compaction1, user2, compaction2 = nid(), nid(), nid()
            ids["compaction1"], ids["user2"], ids["compaction2"] = compaction1, user2, compaction2
            await repository.insert_entry(
                compaction(
                    sid, compaction1, tool1, run_id, user1, 16,
                    details=CompactionDetails.model_validate({"attachments": [reference]}),
                )
            )
            await repository.insert_entry(
                message_entry(
                    sid, user2, compaction1, run_id,
                    UserMessage(role="user", content="追加输入", timestamp=17), 17,
                )
            )
            await repository.insert_entry(compaction(sid, compaction2, user2, run_id, user2, 18))
            steering_id = nid()
            ids["steering"] = steering_id
            await repository.insert_steering(
                SteeringInput(
                    session_id=sid, id=steering_id, run_id=run_id,
                    message=UserMessage(role="user", content="追加输入", timestamp=17),
                    status="consumed", entry_id=user2, reason=None,
                    created_at=17, updated_at=18,
                )
            )
            run = await repository.get_run(sid, run_id)
            await repository.update_run(
                run.model_copy(update={"last_entry_id": compaction2, "status": "completed", "finished_at": 20})
            )
            session = await repository.get_session(sid)
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": compaction2, "updated_at": 20})
            )

        error_session = nid()
        ids["error_session"] = error_session
        async with repository.transaction():
            await repository.insert_session(
                Session(id=error_session, title="失败运行", active_leaf_id=None, created_at=1, updated_at=1)
            )
            error_system, error_user = nid(), nid()
            await repository.insert_entry(
                message_entry(error_session, error_system, None, None, SYSTEM_MESSAGE, 10)
            )
            await repository.insert_entry(
                message_entry(
                    error_session, error_user, error_system, None,
                    UserMessage(role="user", content="失败请求", timestamp=11), 11,
                )
            )
            error_run = nid()
            ids["error_run"] = error_run
            await repository.insert_run(
                SessionRun(
                    session_id=error_session, id=error_run, request_entry_id=error_user,
                    last_entry_id=None, status="failed", started_at=12, finished_at=13,
                    error_code="execution_failed", error_message="执行失败，请重新发起请求。",
                )
            )
            session = await repository.get_session(error_session)
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": error_user, "updated_at": 13})
            )

        empty_session = nid()
        ids["empty_session"] = empty_session
        async with repository.transaction():
            await repository.insert_session(
                Session(id=empty_session, title="空会话", active_leaf_id=None, created_at=1, updated_at=1)
            )

        credential_session = nid()
        ids["credential_session"] = credential_session
        async with repository.transaction():
            await repository.insert_session(
                Session(id=credential_session, title="凭据历史", active_leaf_id=None, created_at=1, updated_at=1)
            )
            cred_system, cred_user = nid(), nid()
            await repository.insert_entry(
                message_entry(credential_session, cred_system, None, None, SYSTEM_MESSAGE, 10)
            )
            await repository.insert_entry(
                message_entry(
                    credential_session, cred_user, cred_system, None,
                    UserMessage(role="user", content="普通输入", timestamp=11), 11,
                )
            )
            cred_run = nid()
            await repository.insert_run(
                SessionRun(
                    session_id=credential_session, id=cred_run, request_entry_id=cred_user,
                    last_entry_id=None, status="running", started_at=12, finished_at=None,
                    error_code=None, error_message=None,
                )
            )
            cred_compaction = nid()
            await repository.insert_entry(
                compaction(
                    credential_session, cred_compaction, cred_user, cred_run, cred_user, 13,
                    summary=f"摘要内命中 {CREDENTIAL_KEY}",
                )
            )
            run = await repository.get_run(credential_session, cred_run)
            await repository.update_run(
                run.model_copy(update={"last_entry_id": cred_compaction, "status": "completed", "finished_at": 14})
            )
            session = await repository.get_session(credential_session)
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": cred_compaction, "updated_at": 14})
            )
    finally:
        await database.close()
    return ids


def projection_checks() -> None:
    sid = nid()
    node = compaction(sid, "c1", "p1", "r1", "k1", 1)
    assert history_entry(node) == {
        "type": "compaction",
        "entry_id": "c1",
        "parent_id": "p1",
        "run_id": "r1",
        "created_at": 1,
    }
    system = message_entry(sid, "sys", None, None, SYSTEM_MESSAGE, 1)
    assert history_entry(system) == {
        "type": "message",
        "entry_id": "sys",
        "parent_id": None,
        "run_id": None,
        "created_at": 1,
        "message": {"role": "system"},
    }
    start = encode(
        AgentEvent(
            "compaction_start",
            {"run_id": "r1", "compaction_id": "c1", "reason": "threshold"},
        )
    )
    assert start == (
        'event: compaction_start\n'
        'data: {"run_id": "r1", "compaction_id": "c1", "reason": "threshold"}\n\n'
    )
    end = encode(
        AgentEvent(
            "compaction_end",
            {"run_id": "r1", "compaction_id": "c1", "entry_id": "c1", "parent_id": "p1"},
        )
    )
    assert end.startswith("event: compaction_end\n")
    try:
        encode(AgentEvent("message_update", {"value": float("nan")}))
    except ValueError:
        pass
    else:
        raise AssertionError("encode 未拒绝非有限数值")


def path_checks() -> None:
    system = message_entry("s1", "sys", None, None, SYSTEM_MESSAGE, 0)
    user = message_entry("s1", "u1", "sys", None, UserMessage(role="user", content="x", timestamp=1), 1)
    node = compaction("s1", "c1", "u1", "r1", "sys", 2)
    assert [entry.id for entry in build_session_path("s1", [system, user, node], "c1")] == [
        "sys",
        "u1",
        "c1",
    ]


def terminal_checks() -> None:
    assert terminal_decision(None, False) == ("completed", None, None)


def http_checks(ids: dict, path: Path) -> dict:
    with Server(app) as server, client(server.base_url) as http:
        sid = ids["session"]
        response = http.get(f"/api/sessions/{sid}/history")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert set(body) == {"session", "entries", "runs", "steering"}
        entries = body["entries"]
        assert [entry["type"] for entry in entries] == [
            "message", "message", "message", "message", "compaction", "message", "compaction",
        ]
        assert [entry["entry_id"] for entry in entries] == [
            ids["system"], ids["user1"], ids["assistant1"], ids["tool1"],
            ids["compaction1"], ids["user2"], ids["compaction2"],
        ]
        assert entries[0]["parent_id"] is None
        for previous, current in zip(entries, entries[1:]):
            assert current["parent_id"] == previous["entry_id"]
        assert body["session"]["active_leaf_id"] == ids["compaction2"]

        for entry in entries:
            assert set(entry) == (COMPACTION_KEYS if entry["type"] == "compaction" else MESSAGE_KEYS)
        assert entries[0]["message"] == {"role": "system"}
        assert entries[1]["message"] == {
            "role": "user", "text": "问题一", "timestamp": 11,
            "attachments": [
                {"attachment_id": ids["attachment"], "file_name": "note.md", "size_bytes": 3, "created_at": 15},
            ],
        }
        assert entries[2]["message"]["role"] == "assistant"
        assert entries[2]["message"]["stop_reason"] == "toolUse"
        assert entries[3]["message"] == {
            "role": "toolResult", "tool_call_id": "call-1", "tool_name": "lookup",
            "content": "结果正文", "is_error": False, "timestamp": 220,
        }

        encoded = json.dumps(entries, ensure_ascii=False)
        for forbidden in (*COMPACTION_FORBIDDEN, "text-sig", "thought_signature"):
            assert forbidden not in encoded, forbidden
        for entry in entries:
            if entry["type"] == "compaction":
                node = json.dumps(entry, ensure_ascii=False)
                assert ids["session"] not in node
                assert ids["attachment"] not in node

        assert len(body["runs"]) == 1
        run = body["runs"][0]
        assert run["run_id"] == ids["run"]
        assert run["request_entry_id"] == ids["user1"]
        assert run["last_entry_id"] == ids["compaction2"]
        assert run["status"] == "completed"

        assert [item["steering_id"] for item in body["steering"]] == [ids["steering"]]
        assert body["steering"][0]["status"] == "consumed"
        assert body["steering"][0]["entry_id"] == ids["user2"]

        run_response = http.get(f"/api/sessions/{sid}/runs/{ids['run']}")
        assert run_response.status_code == 200
        assert run_response.json()["last_entry_id"] == ids["compaction2"]
        assert run_response.json()["status"] == "completed"

        error_body = http.get(f"/api/sessions/{ids['error_session']}/history").json()
        error_run = error_body["runs"][0]
        assert error_run["status"] == "failed"
        assert error_run["error_code"] == "execution_failed"
        assert error_run["error_message"] == "执行失败，请重新发起请求。"
        error_query = http.get(
            f"/api/sessions/{ids['error_session']}/runs/{ids['error_run']}"
        ).json()
        assert (
            error_query["status"],
            error_query["error_code"],
            error_query["error_message"],
        ) == (error_run["status"], error_run["error_code"], error_run["error_message"])

        empty_body = http.get(f"/api/sessions/{ids['empty_session']}/history").json()
        assert empty_body["session"]["active_leaf_id"] is None
        assert empty_body["entries"] == [] and empty_body["runs"] == [] and empty_body["steering"] == []

        blocked = http.get(f"/api/sessions/{ids['credential_session']}/history")
        assert blocked.status_code == 422, blocked.text
        assert blocked.json() == {
            "detail": {"code": "credential_detected", "message": CREDENTIAL_SAFE_MESSAGE}
        }
        assert CREDENTIAL_KEY not in blocked.text

    with sqlite3.connect(path, timeout=30) as raw:
        assert raw.execute(
            "SELECT summary FROM session_entries WHERE id = ?", (ids["compaction1"],)
        ).fetchone()[0] == SUMMARY_TEXT

    return {
        "entry_types": [entry["type"] for entry in entries],
        "credential_status": 422,
    }


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    database_path = TEMP_ROOT / f"{uuid4().hex}.db"
    model_config.DATA_ROOT = TEMP_ROOT / f"model-config-{uuid4().hex}"
    model_config.save_provider(
        api="openai-completions",
        base_url="http://127.0.0.1:9/v1",
        model="model-1",
        provider="example",
        api_key=CREDENTIAL_KEY,
    )
    ids = asyncio.run(seed(database_path))
    database_module.default_database_path = lambda: database_path

    projection_checks()
    path_checks()
    terminal_checks()
    evidence = http_checks(ids, database_path)
    evidence["session"] = ids["session"]
    (ROOT / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 完整祖先链含压缩节点、压缩叶节点与运行末节点、真实用户请求与消费节点、"
        "公开字段白名单与内部字段不泄露、附件与工具结果保真、空历史、"
        "凭据命中、终态记录与公开 error 一致、投影与编码函数断言"
    )


if __name__ == "__main__":
    check()
