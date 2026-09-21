# 阶段 4／5 API 集成测试：唯一 Agent 路由与新增会话路由跑在真实迁移库上，模型是固定替身。
# 依据：CHAT_HISTORY_IMPLEMENTATION_PLAN.md §3.6（请求流程与事件落库顺序）、§3.7（会话 API）；
#       死锁约束：事务内只调 *_in_transaction，普通读先于事务完成。

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langgraph.graph.state import CompiledStateGraph

from app.api import routes_agent
from app.api.dependencies import current_business_date, iso_now
from app.api.middlewares.error_handlers import install_error_handlers
from app.api.routes_conversations import router as conversations_router
from app.api.streaming import stream_frames
from app.application.agent import run_service as run_service_module
from app.application.agent.contracts import (
    AgentEvent,
    AgentRunDeps,
    AgentRuntime,
    GeneratePlanDeps,
)
from app.application.agent.memory import MemoryAssembler
from app.application.agent.prompts import (
    EVALUATOR_SYSTEM_PROMPT,
    GENERAL_CHAT_SYSTEM_PROMPT,
    NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
    NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT,
    PLANNER_SYSTEM_PROMPT,
)
from app.application.agent.router import ROUTER_SYSTEM_PROMPT, FitnessIntent
from app.application.agent.run_service import (
    AGENT_RUN_ERROR_MESSAGE,
    persisted_events,
)
from app.application.ports import MODEL_CALL_FAILED_MESSAGE, TransientModelError
from app.bootstrap import (
    AppServices,
    ReadRepositories,
    SqliteHealthProbe,
    build_repositories,
    build_services,
)
from app.domain.conversations.compaction import (
    SUMMARIZATION_SYSTEM_PROMPT,
    CompactionSettings,
)
from app.domain.conversations.context import (
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    context_messages,
)
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.conversations_repository import (
    ConversationRepo,
)
from app.infrastructure.database.repositories.plans_repository import PlanRepo
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir
from tests.agent.test_agent_run_branches import (
    BUSINESS_DAY,
    ScriptedGateway,
    _extraction_scripts,
    _harness,
    _plan_scripts,
    _tool_harnesses,
    final_answer,
    tool_call,
)

FIRST_REQUEST = "第一轮：训练日怎么安排"
FIRST_ANSWER = "第一轮回答：先练胸"
SECOND_REQUEST = "第二轮：改成周三"
SECOND_ANSWER = "第二轮回答：已按周三日程理解"
NL_REQUEST = "今天做了 8 个引体向上"
NL_SUMMARY = "解析结果：自重引体向上 1 组 8 次，待你确认后写入"
PLAN_REQUEST = "帮我生成一份训练计划"
PROGRESS_REQUEST = "看看我的进步"
KNOWLEDGE_REQUEST = "新手一周练几次合适？"
#: 压缩闭环用例：足量用户文本保证上下文规模确实越过阈值，切点因此落在第二轮请求上。
LONG_SECOND_REQUEST = "第二轮：改成周三" + "甲" * 8000
THIRD_REQUEST = "第三轮：再调整一次"
THIRD_ANSWER = "第三轮回答：已记录"
COMPACTION_SUMMARY = "## 用户目标\n- 减脂"
COMPACTION_WINDOW_TOKENS = 1000
COMPACTION_SETTINGS = CompactionSettings(reserve_tokens=100, keep_recent_tokens=100)
THREAD_1 = "11111111-1111-1111-1111-111111111111"
THREAD_2 = "22222222-2222-2222-2222-222222222222"
THREAD_3 = "44444444-4444-4444-4444-444444444444"
THREAD_OTHER = "33333333-3333-3333-3333-333333333333"


def _app(harness_graph: CompiledStateGraph, db: Database, model: ScriptedGateway) -> FastAPI:
    app = FastAPI(lifespan=None)
    install_error_handlers(app)
    app.include_router(routes_agent.router)
    app.include_router(conversations_router)
    app.state.db = db
    repositories = build_repositories(db)
    services = build_services(
        repositories, db, SqliteHealthProbe(db, db.path.parent)
    )
    app.state.services = services
    app.state.agent_runtime = AgentRuntime(
        graph=harness_graph,
        deps=_deps(repositories, services, model),
        run_deps=AgentRunDeps(
            model=model,
            stats=services.stats,
            plans=repositories.plans,
            persistence=services.plan_persistence,
            catalog=repositories.exercises,
            records=services.records,
            skills=SkillLoader(skills_dir()),
            tool_harnesses=_tool_harnesses(),
        ),
    )
    app.dependency_overrides[current_business_date] = lambda: BUSINESS_DAY
    return app


def _deps(
    repositories: ReadRepositories, services: AppServices, model: ScriptedGateway
) -> GeneratePlanDeps:
    return GeneratePlanDeps(
        profiles=services.profile,
        catalog=repositories.exercises,
        stats=repositories.stats,
        assembler=MemoryAssembler(
            profiles=repositories.profiles,
            plans=repositories.plans,
            records=services.records,
            stats=services.stats,
        ),
        skills=SkillLoader(skills_dir()),
        persistence=services.plan_persistence,
        plans=repositories.plans,
        activation=services.plan_activation,
        model=model,
        now=lambda: datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
    )


@asynccontextmanager
async def _client(
    tmp_path: Any,
    *,
    structured: Mapping[type, Sequence[Mapping[str, Any]]] | None = None,
    text: Mapping[str, Sequence[str]] | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, ScriptedGateway, Database]]:
    async with _harness(tmp_path, structured=structured, text=text) as harness:
        app = _app(harness.graph, harness.db, harness.model)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost"
        ) as client:
            yield client, harness.model, harness.db


def _frames(body: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE 文本 → (事件名, 载荷) 序列。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        name = lines[0].removeprefix("event: ")
        frames.append((name, json.loads(lines[1].removeprefix("data: "))))
    return frames


def _general_scripts() -> dict[type, list[Mapping[str, Any]]]:
    return {
        FitnessIntent: [
            {"domain": "general", "action": "chat"},
            {"domain": "general", "action": "chat"},
        ]
    }


def _natural_language_scripts() -> dict[type, list[Mapping[str, Any]]]:
    """自然语言打卡的结构化响应：路由 ＋ 一次自重提取。"""
    return {
        FitnessIntent: [
            {
                "domain": "workout_execution",
                "action": "create",
                "execution_type": "natural_language_record",
            }
        ],
        **_extraction_scripts(),
    }


async def _call_with_disconnect(
    asgi: Callable[..., Awaitable[None]],
    *,
    path: str,
    body: bytes,
    disconnect_after: int,
    suspend_send: bool = False,
) -> list[str]:
    """直连 ASGI 应用：第 ``disconnect_after`` 个响应分片送抵客户端后投递 ``http.disconnect``。

    ``httpx.ASGITransport`` 不能在流中投递断连，所以按 ASGI server 的语义自己实现 receive：
    断连到达后 ``StreamingResponse`` 的监听任务结束并取消分派任务组，正在生成器内的等待
    因此收到真实的 AnyIO 取消（非测试注入的异常）。

    ``suspend_send``：该分片的 ``send`` 用永不放行的等待挂住（客户端读得慢的真实形态），
    取消只能落在生成器交出帧之后的下游 ``send`` 上，不会进入生成器帧。
    """
    request_delivered = False
    disconnected = asyncio.Event()
    stalled = asyncio.Event()
    chunks: list[str] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        chunk = message.get("body")
        if message["type"] == "http.response.body" and chunk:
            chunks.append(str(chunk.decode()))
            if len(chunks) == disconnect_after:
                disconnected.set()
                if suspend_send:
                    await stalled.wait()

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"localhost"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 4321),
        "server": ("localhost", 8000),
    }
    await asgi(scope, receive, send)
    return chunks


async def _create_conversation(client: httpx.AsyncClient, title: str) -> str:
    created = await client.post("/api/conversations", json={"title": title})
    assert created.status_code == 201
    return str(created.json()["id"])


def _run_body(
    *, chat_id: str, thread_id: str, request: str, client_request_id: str
) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "conversation_id": thread_id,
        "client_request_id": client_request_id,
        "request": request,
    }


async def _run(
    client: httpx.AsyncClient,
    *,
    chat_id: str,
    thread_id: str,
    request: str,
    client_request_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    response = await client.post(
        "/api/agent/run",
        json=_run_body(
            chat_id=chat_id,
            thread_id=thread_id,
            request=request,
            client_request_id=client_request_id,
        ),
    )
    assert response.status_code == 200
    return _frames(response.text)


def _waiting_draft_plan_id(frames: Sequence[tuple[str, dict[str, Any]]]) -> int:
    return int([data["draft_plan_id"] for name, data in frames if name == "waiting"][0])


async def _confirm(
    client: httpx.AsyncClient, *, chat_id: str, thread_id: str, plan_id: int
) -> httpx.Response:
    return await client.post(
        "/api/agent/confirm",
        json={"chat_id": chat_id, "conversation_id": thread_id, "plan_id": plan_id},
    )


async def _reject(
    client: httpx.AsyncClient, *, chat_id: str, thread_id: str, plan_id: int
) -> httpx.Response:
    return await client.post(
        "/api/agent/reject",
        json={"chat_id": chat_id, "conversation_id": thread_id, "plan_id": plan_id},
    )


async def _create_plan_round(
    client: httpx.AsyncClient, *, chat_id: str, thread_id: str, client_request_id: str
) -> int:
    """跑一轮生成计划：返回停在确认 interrupt 的 draft 身份。"""
    frames = await _run(
        client,
        chat_id=chat_id,
        thread_id=thread_id,
        request=PLAN_REQUEST,
        client_request_id=client_request_id,
    )
    return _waiting_draft_plan_id(frames)


async def test_conversation_api_creates_lists_reads_and_deletes(tmp_path: Any) -> None:
    async with _client(tmp_path) as (client, _model, _db):
        chat_id = await _create_conversation(client, "会话甲")
        listed = await client.get("/api/conversations")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["conversations"]] == [chat_id]

        detail = await client.get(f"/api/conversations/{chat_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["conversation"]["title"] == "会话甲"
        assert body["rounds"] == []
        assert body["compactions"] == []
        assert set(body["conversation"]) == {"id", "title", "created_at", "updated_at"}

        deleted = await client.delete(f"/api/conversations/{chat_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}
        assert (await client.get(f"/api/conversations/{chat_id}")).status_code == 404


async def test_unknown_and_illegal_conversation_ids_share_one_error_shape(
    tmp_path: Any,
) -> None:
    async with _client(tmp_path) as (client, _model, _db):
        missing = await client.get("/api/conversations/00000000-0000-0000-0000-000000000000")
        illegal = await client.get("/api/conversations/not-a-uuid")
        assert missing.status_code == illegal.status_code == 404
        assert missing.json() == illegal.json()
        assert missing.json()["http_status"] == 404
        assert (await client.delete("/api/conversations/not-a-uuid")).status_code == 404


async def test_agent_run_persists_entries_and_events_before_streaming(
    tmp_path: Any,
) -> None:
    async with _client(
        tmp_path,
        structured=_general_scripts(),
        harness=_general_harness([FIRST_ANSWER]),
    ) as (client, _model, db):
        chat_id = await _create_conversation(client, "会话乙")
        frames = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        assert [name for name, _ in frames] == ["node", "message", "done"]
        assert frames[1][1] == {"text": FIRST_ANSWER}

        repo = ConversationRepo(db)
        runs = await repo.list_runs(chat_id)
        assert [run.status for run in runs] == ["completed"]
        assert runs[0].client_request_id == "request-1"
        assert runs[0].thread_id == THREAD_1
        events = await repo.list_run_events(runs[0].id)
        assert [event.event_type for event in events] == ["node", "message", "done"]
        assert [event.sequence for event in events] == [1, 2, 3]
        assistant_entry_id = runs[0].assistant_entry_id
        assert assistant_entry_id is not None
        entries = await repo.list_entries(chat_id)
        assert [entry.payload["role"] for entry in entries] == ["user", "assistant"]
        assert entries[1].payload["content"] == FIRST_ANSWER
        assert entries[1].payload["status"] == "complete"

        detail = (await client.get(f"/api/conversations/{chat_id}")).json()
        assert [round_["request"] for round_ in detail["rounds"]] == [FIRST_REQUEST]
        assert detail["rounds"][0]["assistants"][0]["content"] == FIRST_ANSWER
        assert [event["event"] for event in detail["rounds"][0]["events"]] == [
            "node",
            "message",
            "done",
        ]


async def test_second_round_receives_history_and_current_request_once(
    tmp_path: Any,
) -> None:
    async with _client(
        tmp_path,
        structured=_general_scripts(),
        harness=_general_harness([FIRST_ANSWER, SECOND_ANSWER]),
    ) as (client, model, _db):
        chat_id = await _create_conversation(client, "会话丙")
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_2,
            request=SECOND_REQUEST,
            client_request_id="request-2",
        )

        router_payloads = [
            json.loads(payload)
            for prompt, payload in model.calls
            if prompt == ROUTER_SYSTEM_PROMPT
        ]
        assert router_payloads[0] == {"request": FIRST_REQUEST}
        second = router_payloads[1]
        assert second["request"] == SECOND_REQUEST
        assert second["conversation_messages"] == [
            {"role": "user", "text": FIRST_REQUEST},
            {"role": "assistant", "text": FIRST_ANSWER},
        ]
        general_payload = json.loads(
            [payload for prompt, payload in model.calls if prompt == GENERAL_CHAT_SYSTEM_PROMPT][-1]
        )
        assert general_payload["conversation_messages"] == [
            {"role": "user", "text": FIRST_REQUEST},
            {"role": "assistant", "text": FIRST_ANSWER},
        ]


async def test_replay_of_the_same_client_request_id_reuses_persisted_events(
    tmp_path: Any,
) -> None:
    async with _client(
        tmp_path,
        structured=_general_scripts(),
        harness=_general_harness([FIRST_ANSWER]),
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话丁")
        first = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        calls_after_first = len(model.calls)
        replay = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        assert replay == first
        assert len(model.calls) == calls_after_first
        repo = ConversationRepo(db)
        assert len(await repo.list_runs(chat_id)) == 1
        assert len(await repo.list_entries(chat_id)) == 2
        detail = (await client.get(f"/api/conversations/{chat_id}")).json()
        round_ = detail["rounds"][0]
        assert {"conversation_id", "request", "events"} <= set(round_)
        assert round_["conversation_id"] == THREAD_1
        assert [event["event"] for event in round_["events"]] == [
            "node",
            "message",
            "done",
        ]


async def test_client_request_id_replay_rejects_another_chat_or_thread(
    tmp_path: Any,
) -> None:
    """幂等键重放必须属于同一会话与同一 thread：不匹配即标准 400，零新增写入。"""
    async with _client(
        tmp_path,
        structured=_general_scripts(),
        harness=_general_harness([FIRST_ANSWER]),
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话子")
        other_chat_id = await _create_conversation(client, "会话丑")
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        calls = len(model.calls)
        repo = ConversationRepo(db)
        entries = len(await repo.list_entries(chat_id))

        for body in (
            _run_body(
                chat_id=other_chat_id,
                thread_id=THREAD_1,
                request=FIRST_REQUEST,
                client_request_id="request-1",
            ),
            _run_body(
                chat_id=chat_id,
                thread_id=THREAD_OTHER,
                request=FIRST_REQUEST,
                client_request_id="request-1",
            ),
        ):
            rejected = await client.post("/api/agent/run", json=body)
            assert rejected.status_code == 400
            assert rejected.json()["error_code"] == "invalid_request"

        assert len(model.calls) == calls
        assert len(await repo.list_runs(chat_id)) == 1
        assert len(await repo.list_entries(chat_id)) == entries
        assert await repo.list_runs(other_chat_id) == ()


async def test_concurrent_duplicate_requests_execute_the_model_once(
    tmp_path: Any,
) -> None:
    """并发重放：两个同 chat／同 thread 的重复请求只调一次模型，只建一条用户 Entry 与一个 Run。"""
    async with _client(
        tmp_path,
        structured=_general_scripts(),
        harness=_general_harness([FIRST_ANSWER]),
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话寅")
        body = _run_body(
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        first, second = await asyncio.gather(
            client.post("/api/agent/run", json=body),
            client.post("/api/agent/run", json=body),
        )

        assert first.status_code == second.status_code == 200
        assert FIRST_ANSWER in first.text + second.text
        assert len(model.calls) == 2
        repo = ConversationRepo(db)
        runs = await repo.list_runs(chat_id)
        assert len(runs) == 1
        assert len(await repo.list_entries(chat_id)) == 2
        assert len(await repo.list_run_events(runs[0].id)) == 3


async def test_transaction_race_replay_reuses_the_run_after_a_pre_read_miss(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预读未命中、事务内发现同幂等键的路径：校验身份后原样重放，不调模型、不重复写。"""
    async with _client(
        tmp_path,
        structured=_general_scripts(),
        harness=_general_harness([FIRST_ANSWER]),
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话卯")
        first = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        calls = len(model.calls)

        async def _pre_read_miss(
            _self: ConversationRepo, _client_request_id: str
        ) -> None:
            return None

        monkeypatch.setattr(
            ConversationRepo, "read_run_by_client_request_id", _pre_read_miss
        )
        raced = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        assert raced == first
        assert len(model.calls) == calls
        repo = ConversationRepo(db)
        assert len(await repo.list_runs(chat_id)) == 1
        assert len(await repo.list_entries(chat_id)) == 2

        # 事务内才发现的身份不匹配：仓库层幂等守卫就地失败（与 test_conversation_repo 同一不变式），
        # 不留任何写入。
        with pytest.raises(ValueError):
            await client.post(
                "/api/agent/run",
                json=_run_body(
                    chat_id=chat_id,
                    thread_id=THREAD_OTHER,
                    request=FIRST_REQUEST,
                    client_request_id="request-1",
                ),
            )
        assert len(await repo.list_runs(chat_id)) == 1
        assert len(await repo.list_entries(chat_id)) == 2

async def test_agent_run_rejects_unknown_conversation(tmp_path: Any) -> None:
    async with _client(tmp_path) as (client, model, _db):
        before = len(model.calls)
        response = await client.post(
            "/api/agent/run",
            json={
                "chat_id": "00000000-0000-0000-0000-000000000000",
                "conversation_id": THREAD_1,
                "client_request_id": "request-1",
                "request": FIRST_REQUEST,
            },
        )
        assert response.status_code == 404
        assert response.json()["http_status"] == 404
        assert len(model.calls) == before


async def test_each_semantic_event_is_committed_before_its_frame_is_yielded(
    tmp_path: Any,
) -> None:
    """事件落库必须先于 SSE 帧：帧被消费时数据库里已有对应 Event（plan §7）。"""
    async with _harness(tmp_path) as harness:
        repo = ConversationRepo(harness.db)
        conversation = await repo.create_conversation(
            conversation_id="11111111-2222-3333-4444-555555555555",
            title="会话己",
            created_at="2026-06-01T08:00:00+00:00",
        )
        async with harness.db.transaction() as conn:
            run, _user_entry = await repo.begin_run_in_transaction(
                conn,
                conversation_id=conversation.id,
                run_id="run-1",
                thread_id=THREAD_1,
                client_request_id="request-1",
                entry_id="entry-user",
                content=FIRST_REQUEST,
                created_at="2026-06-01T08:00:00+00:00",
            )
            await repo.update_run_status_in_transaction(
                conn, run.id, status="running", updated_at="2026-06-01T08:00:01+00:00"
            )

        async def events() -> AsyncIterator[AgentEvent]:
            yield AgentEvent("node", {"name": "general"})
            yield AgentEvent("message", {"text": FIRST_ANSWER})
            yield AgentEvent("done", {"ok": True, "intent": "general"})

        seen: list[tuple[str, list[str]]] = []
        async for frame in stream_frames(
            persisted_events(
                harness.db,
                repo,
                run.id,
                events(),
                now=iso_now,
            )
        ):
            persisted = await repo.list_run_events(run.id)
            seen.append((frame.splitlines()[0], [event.event_type for event in persisted]))
        assert seen == [
            ("event: node", ["node"]),
            ("event: message", ["node", "message"]),
            ("event: done", ["node", "message", "done"]),
        ]
        finished = await repo.read_run(run.id)
        assert finished is not None and finished.status == "completed"


async def test_failing_run_is_persisted_as_failed_with_an_error_event(
    tmp_path: Any,
) -> None:
    async with _harness(tmp_path) as harness:
        repo = ConversationRepo(harness.db)
        conversation = await repo.create_conversation(
            conversation_id="11111111-2222-3333-4444-666666666666",
            title="会话庚",
            created_at="2026-06-01T08:00:00+00:00",
        )
        async with harness.db.transaction() as conn:
            run, _user_entry = await repo.begin_run_in_transaction(
                conn,
                conversation_id=conversation.id,
                run_id="run-1",
                thread_id=THREAD_1,
                client_request_id="request-1",
                entry_id="entry-user",
                content=FIRST_REQUEST,
                created_at="2026-06-01T08:00:00+00:00",
            )
            await repo.update_run_status_in_transaction(
                conn, run.id, status="running", updated_at="2026-06-01T08:00:01+00:00"
            )

        async def events() -> AsyncIterator[AgentEvent]:
            yield AgentEvent("node", {"name": "general"})
            raise RuntimeError("模型调用失败")

        frames = [
            frame
            async for frame in stream_frames(
                persisted_events(
                    harness.db,
                    repo,
                    run.id,
                    events(),
                    now=iso_now,
                )
            )
        ]
        assert frames[-1].splitlines()[0] == "event: error"
        failed = await repo.read_run(run.id)
        assert failed is not None and failed.status == "failed"
        assert failed.error_code == "RuntimeError"
        persisted = await repo.list_run_events(run.id)
        assert [event.event_type for event in persisted] == ["node", "error"]
        assert [event.sequence for event in persisted] == [1, 2]


async def test_transient_provider_failure_keeps_the_model_call_error_text(
    tmp_path: Any,
) -> None:
    """Provider 瞬时故障（重试耗尽后上抛）：可见文本仍是模型调用失败文案，Run 落库 failed。"""
    async with _harness(tmp_path) as harness:
        repo = ConversationRepo(harness.db)
        conversation = await repo.create_conversation(
            conversation_id="11111111-2222-3333-4444-888888888888",
            title="会话子",
            created_at="2026-06-01T08:00:00+00:00",
        )
        async with harness.db.transaction() as conn:
            run, _user_entry = await repo.begin_run_in_transaction(
                conn,
                conversation_id=conversation.id,
                run_id="run-1",
                thread_id=THREAD_1,
                client_request_id="request-1",
                entry_id="entry-user",
                content=FIRST_REQUEST,
                created_at="2026-06-01T08:00:00+00:00",
            )
            await repo.update_run_status_in_transaction(
                conn, run.id, status="running", updated_at="2026-06-01T08:00:01+00:00"
            )

        async def events() -> AsyncIterator[AgentEvent]:
            yield AgentEvent("node", {"name": "general"})
            raise TransientModelError(MODEL_CALL_FAILED_MESSAGE)

        frames = [
            frame
            async for frame in stream_frames(
                persisted_events(
                    harness.db,
                    repo,
                    run.id,
                    events(),
                    now=iso_now,
                )
            )
        ]
        assert frames[-1].splitlines()[0] == "event: error"
        assert json.loads(frames[-1].splitlines()[1].removeprefix("data: ")) == {
            "message": MODEL_CALL_FAILED_MESSAGE
        }
        failed = await repo.read_run(run.id)
        assert failed is not None and failed.status == "failed"
        assert failed.error_code == "TransientModelError"


async def test_client_disconnect_cancels_the_active_run(tmp_path: Any) -> None:
    """真实 ASGI 断连（spec 2.3 任务组）：活动 Run 收敛为 cancelled，已提交事件保留，
    不发 error 帧、不写 Assistant Entry。"""
    async with _harness(
        tmp_path,
        structured=_general_scripts(),
        harness=_general_harness([FIRST_ANSWER]),
    ) as harness:
        app = _app(harness.graph, harness.db, harness.model)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            chat_id = await _create_conversation(client, "会话癸")
        repo = ConversationRepo(harness.db)
        body = json.dumps(
            _run_body(
                chat_id=chat_id,
                thread_id=THREAD_1,
                request=FIRST_REQUEST,
                client_request_id="request-1",
            )
        ).encode()
        # 分派任务组把生成器抛出的取消当作自身取消吞下：ASGI 调用正常返回，不发生未处理异常。
        chunks = await _call_with_disconnect(
            app,
            path="/api/agent/run",
            body=body,
            disconnect_after=1,
        )

        frames = _frames("".join(chunks))
        run = (await repo.list_runs(chat_id))[0]
        assert run.status == "cancelled"
        assert run.error_code is None
        assert run.assistant_entry_id is None
        # 先落库再发送：已送出的每个分片都已提交，取消不回滚已提交事件、不追加 error。
        persisted = [event.event_type for event in await repo.list_run_events(run.id)]
        assert persisted[: len(frames)] == [name for name, _ in frames]
        assert "error" not in persisted
        entries = await repo.list_entries(chat_id)
        assert [entry.entry_type for entry in entries] == ["message"]
        assert entries[0].payload["role"] == "user"


async def test_disconnect_in_suspended_send_before_the_waiting_commit_cancels_the_run(
    tmp_path: Any,
) -> None:
    """断连落在下游 ``send`` 上（send 永不放行）且确认点尚未提交：取消不进入生成器帧，
    响应边界仍把 ``running`` Run 收敛为 ``cancelled``，已提交事件保留、不发 error 帧、不写 Assistant Entry。
    """
    async with _harness(
        tmp_path,
        structured=_natural_language_scripts(),
        text={NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: [NL_SUMMARY]},
    ) as harness:
        app = _app(harness.graph, harness.db, harness.model)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            chat_id = await _create_conversation(client, "会话子")
        repo = ConversationRepo(harness.db)
        body = json.dumps(
            _run_body(
                chat_id=chat_id,
                thread_id=THREAD_1,
                request=NL_REQUEST,
                client_request_id="request-1",
            )
        ).encode()
        # 断连在第一个分片的 send 内挂起等待时到达：生成器停在 yield 上、收不到取消，
        # 收敛只能由响应自己的 send 边界完成；ASGI 调用返回即证明收敛已跑完。
        chunks = await _call_with_disconnect(
            app,
            path="/api/agent/run",
            body=body,
            disconnect_after=1,
            suspend_send=True,
        )

        frames = _frames("".join(chunks))
        assert [name for name, _ in frames] == ["node"]
        run = (await repo.list_runs(chat_id))[0]
        assert run.status == "cancelled"
        assert run.error_code is None
        assert run.assistant_entry_id is None
        persisted = [event.event_type for event in await repo.list_run_events(run.id)]
        assert persisted == [name for name, _ in frames]
        entries = await repo.list_entries(chat_id)
        assert [entry.entry_type for entry in entries] == ["message"]


async def test_disconnect_in_suspended_send_after_the_waiting_commit_keeps_waiting(
    tmp_path: Any,
) -> None:
    """断连落在下游 ``send`` 上且 ``waiting`` 已与确认点同事务提交：Run 保持 ``waiting``，
    已提交事件（含恢复确认流程所需的 waiting 载荷）全部保留，不发 error 帧、不写 Assistant Entry。
    """
    async with _harness(
        tmp_path,
        structured=_natural_language_scripts(),
        text={NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: [NL_SUMMARY]},
    ) as harness:
        app = _app(harness.graph, harness.db, harness.model)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            chat_id = await _create_conversation(client, "会话丑")
        repo = ConversationRepo(harness.db)
        body = json.dumps(
            _run_body(
                chat_id=chat_id,
                thread_id=THREAD_1,
                request=NL_REQUEST,
                client_request_id="request-1",
            )
        ).encode()
        # 第三个分片是 waiting：它的 Event 与 Run ``waiting`` 先落库，send 才拿到这一帧。
        chunks = await _call_with_disconnect(
            app,
            path="/api/agent/run",
            body=body,
            disconnect_after=3,
            suspend_send=True,
        )

        frames = _frames("".join(chunks))
        assert [name for name, _ in frames] == ["node", "message", "waiting"]
        run = (await repo.list_runs(chat_id))[0]
        assert run.status == "waiting"
        assert run.error_code is None
        assert run.assistant_entry_id is None
        persisted = await repo.list_run_events(run.id)
        assert [event.event_type for event in persisted] == [name for name, _ in frames]
        assert persisted[-1].payload == frames[-1][1]
        assert [entry.entry_type for entry in await repo.list_entries(chat_id)] == [
            "message"
        ]
        # 刷新恢复：会话读回仍按 waiting 报告该轮，确认流程据同一份载荷继续。
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            detail = (await client.get(f"/api/conversations/{chat_id}")).json()
        assert detail["rounds"][0]["status"] == "waiting"


async def test_disconnect_after_done_keeps_the_completed_run(tmp_path: Any) -> None:
    """done 已落库后断连：Run 保持 completed，不尝试终态 -> cancelled 的非法迁移。"""
    async with _harness(tmp_path) as harness:
        repo = ConversationRepo(harness.db)
        conversation = await repo.create_conversation(
            conversation_id="11111111-2222-3333-4444-888888888888",
            title="会话壬",
            created_at="2026-06-01T08:00:00+00:00",
        )
        async with harness.db.transaction() as conn:
            run, _user_entry = await repo.begin_run_in_transaction(
                conn,
                conversation_id=conversation.id,
                run_id="run-1",
                thread_id=THREAD_1,
                client_request_id="request-1",
                entry_id="entry-user",
                content=FIRST_REQUEST,
                created_at="2026-06-01T08:00:00+00:00",
            )
            await repo.update_run_status_in_transaction(
                conn, run.id, status="running", updated_at="2026-06-01T08:00:01+00:00"
            )

        released = asyncio.Event()

        async def events() -> AsyncIterator[AgentEvent]:
            yield AgentEvent("message", {"text": FIRST_ANSWER})
            yield AgentEvent("done", {"ok": True, "intent": "general"})
            # done 已与终态同事务提交，此处仍挂着：断连到达时 Run 已是 completed。
            await released.wait()

        response = StreamingResponse(
            stream_frames(
                persisted_events(
                    harness.db,
                    repo,
                    run.id,
                    events(),
                    now=iso_now,
                )
            ),
            media_type="text/event-stream",
        )
        chunks = await _call_with_disconnect(
            response,
            path="/api/agent/run",
            body=b"",
            disconnect_after=2,
        )

        assert [name for name, _ in _frames("".join(chunks))] == ["message", "done"]
        completed = await repo.read_run(run.id)
        assert completed is not None and completed.status == "completed"
        assert completed.error_code is None
        assert completed.assistant_entry_id is not None
        persisted = [event.event_type for event in await repo.list_run_events(run.id)]
        assert persisted == ["message", "done"]
        assert [entry.entry_type for entry in await repo.list_entries(conversation.id)] == [
            "message",
            "message",
        ]


async def test_blank_non_waiting_answer_fails_the_run_without_committing_done(
    tmp_path: Any,
) -> None:
    """非 waiting 的 Run 收到空白可见输出：done 不落库、不发 done 帧，只发净化后的 error。"""
    async with _harness(tmp_path) as harness:
        repo = ConversationRepo(harness.db)
        conversation = await repo.create_conversation(
            conversation_id="11111111-2222-3333-4444-777777777777",
            title="会话辛",
            created_at="2026-06-01T08:00:00+00:00",
        )
        async with harness.db.transaction() as conn:
            run, _user_entry = await repo.begin_run_in_transaction(
                conn,
                conversation_id=conversation.id,
                run_id="run-1",
                thread_id=THREAD_1,
                client_request_id="request-1",
                entry_id="entry-user",
                content=FIRST_REQUEST,
                created_at="2026-06-01T08:00:00+00:00",
            )
            await repo.update_run_status_in_transaction(
                conn, run.id, status="running", updated_at="2026-06-01T08:00:01+00:00"
            )

        async def events() -> AsyncIterator[AgentEvent]:
            yield AgentEvent("message", {"text": "   "})
            yield AgentEvent("done", {"ok": True, "intent": "general"})

        frames = [
            frame
            async for frame in stream_frames(
                persisted_events(
                    harness.db,
                    repo,
                    run.id,
                    events(),
                    now=iso_now,
                )
            )
        ]
        assert [frame.splitlines()[0] for frame in frames] == [
            "event: message",
            "event: error",
        ]
        assert AGENT_RUN_ERROR_MESSAGE in frames[-1]

        persisted = await repo.list_run_events(run.id)
        assert [event.event_type for event in persisted] == ["message", "error"]
        assert [event.sequence for event in persisted] == [1, 2]

        failed = await repo.read_run(run.id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "InvalidConversationPayload"
        assert failed.assistant_entry_id is None
        assert [
            entry.payload["role"] for entry in await repo.list_entries(conversation.id)
        ] == ["user"]


async def test_model_backed_blank_answer_becomes_a_client_visible_error(
    tmp_path: Any,
) -> None:
    """模型分支返回空白文本：客户端终态事件是 error，Run failed，不落 Assistant Entry。"""
    async with _client(
        tmp_path,
        structured=_general_scripts(),
        harness=_general_harness(["   "]),
    ) as (client, _model, db):
        chat_id = await _create_conversation(client, "会话壬")
        frames = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        assert [name for name, _ in frames] == ["node", "message", "error"]
        assert frames[1][1] == {"text": ""}
        assert frames[2][1] == {"message": AGENT_RUN_ERROR_MESSAGE}

        repo = ConversationRepo(db)
        runs = await repo.list_runs(chat_id)
        assert [run.status for run in runs] == ["failed"]
        assert runs[0].assistant_entry_id is None
        persisted = await repo.list_run_events(runs[0].id)
        assert [event.event_type for event in persisted] == ["node", "message", "error"]
        assert [event.sequence for event in persisted] == [1, 2, 3]
        assert [entry.payload["role"] for entry in await repo.list_entries(chat_id)] == [
            "user"
        ]

        detail = (await client.get(f"/api/conversations/{chat_id}")).json()
        assert [
            event["event"] for event in detail["rounds"][0]["events"]
        ] == ["node", "message", "error"]


async def test_confirmation_binds_the_exact_waiting_run_when_a_newer_run_exists(
    tmp_path: Any,
) -> None:
    """确认绑定 ``conversation_id`` 指定的来源 Run：更晚的 Run 存在时也不落到“最近一轮”。"""
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [
                {"domain": "plan_management", "action": "create"},
                {"domain": "general", "action": "chat"},
            ],
            **_plan_scripts([True]),
        },
        harness=_general_harness([SECOND_ANSWER]),
    ) as (client, _model, db):
        chat_id = await _create_conversation(client, "会话戊")
        plan_id = await _create_plan_round(
            client, chat_id=chat_id, thread_id=THREAD_1, client_request_id="request-1"
        )
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_2,
            request=SECOND_REQUEST,
            client_request_id="request-2",
        )
        repo = ConversationRepo(db)
        runs = {run.thread_id: run for run in await repo.list_runs(chat_id)}
        assert runs[THREAD_1].status == "waiting"
        assert runs[THREAD_2].status == "completed"

        confirmed = await _confirm(
            client, chat_id=chat_id, thread_id=THREAD_1, plan_id=plan_id
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["plan"]["status"] == "active"
        replayed = await _confirm(
            client, chat_id=chat_id, thread_id=THREAD_1, plan_id=plan_id
        )
        assert replayed.status_code == 200
        assert replayed.json() == confirmed.json()

        confirmations = [
            entry
            for entry in await repo.list_entries(chat_id)
            if entry.entry_type == "confirmation"
        ]
        assert len(confirmations) == 1
        assert confirmations[0].payload["action"] == "plan_confirmed"
        assert confirmations[0].payload["draft_plan_id"] == plan_id
        assert confirmations[0].payload["run_id"] == runs[THREAD_1].id

        detail = (await client.get(f"/api/conversations/{chat_id}")).json()
        rounds = {round_["conversation_id"]: round_ for round_ in detail["rounds"]}
        assert set(rounds) == {THREAD_1, THREAD_2}
        assert [item["action"] for item in rounds[THREAD_1]["confirmations"]] == [
            "plan_confirmed"
        ]
        assert rounds[THREAD_2]["confirmations"] == []
        assert rounds[THREAD_2]["request"] == SECOND_REQUEST
        events = [event["event"] for event in rounds[THREAD_1]["events"]]
        assert events[-2:] == ["waiting", "done"]
        assert "message" not in events

        projected = context_messages(await repo.list_entries(chat_id))
        assert projected[-1].role == "user"
        assert projected[-1].text == confirmations[0].payload["text"]


async def test_repeated_plan_reject_appends_one_entry(tmp_path: Any) -> None:
    """拒绝重放：draft 归档一次，确认 Entry 只追加一条，不产生 active。"""
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [{"domain": "plan_management", "action": "create"}],
            **_plan_scripts([True]),
        },
    ) as (client, _model, db):
        chat_id = await _create_conversation(client, "会话己")
        plan_id = await _create_plan_round(
            client, chat_id=chat_id, thread_id=THREAD_1, client_request_id="request-1"
        )
        first = await _reject(
            client, chat_id=chat_id, thread_id=THREAD_1, plan_id=plan_id
        )
        second = await _reject(
            client, chat_id=chat_id, thread_id=THREAD_1, plan_id=plan_id
        )
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["plan"]["status"] == "archived"

        repo = ConversationRepo(db)
        confirmations = [
            entry
            for entry in await repo.list_entries(chat_id)
            if entry.entry_type == "confirmation"
        ]
        assert [entry.payload["action"] for entry in confirmations] == [
            "plan_rejected"
        ]
        assert await PlanRepo(db).read_active() is None


async def test_confirmation_mismatch_performs_zero_business_writes(
    tmp_path: Any,
) -> None:
    """确认三端点都在任何业务写入之前校验精确来源 Run：chat／thread 不匹配即零写入。"""
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [{"domain": "plan_management", "action": "create"}],
            **_plan_scripts([True]),
        },
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话庚")
        other_chat_id = await _create_conversation(client, "会话辛")
        plan_id = await _create_plan_round(
            client, chat_id=chat_id, thread_id=THREAD_1, client_request_id="request-1"
        )
        calls = len(model.calls)
        repo = ConversationRepo(db)
        workout_body = {
            "performed_on": "2026-06-01",
            "sets": [
                {
                    "exercise_id": "pull-up",
                    "set_no": 1,
                    "set_type": "work",
                    "reps": 8,
                }
            ],
            "plan_session_id": None,
            "auto_link": False,
        }

        for chat, thread in ((chat_id, THREAD_OTHER), (other_chat_id, THREAD_1)):
            for response in (
                await _confirm(client, chat_id=chat, thread_id=thread, plan_id=plan_id),
                await _reject(client, chat_id=chat, thread_id=thread, plan_id=plan_id),
                await client.post(
                    "/api/agent/confirm-workout",
                    json={"chat_id": chat, "conversation_id": thread, **workout_body},
                ),
            ):
                assert response.status_code == 400
                assert response.json()["error_code"] == "invalid_request"

        assert len(model.calls) == calls
        plan = await PlanRepo(db).read_by_id(plan_id)
        assert plan is not None and plan.status == "draft"
        assert await PlanRepo(db).read_active() is None
        assert (
            await build_services(
                build_repositories(db),
                db,
                SqliteHealthProbe(db, db.path.parent),
            ).records.list_all()
        ) == ()
        assert [
            entry for entry in await repo.list_entries(chat_id)
            if entry.entry_type == "confirmation"
        ] == []
        assert await repo.list_entries(other_chat_id) == ()


async def test_natural_language_record_waiting_run_stays_waiting(
    tmp_path: Any,
) -> None:
    """发出 ``waiting`` 的运行在 ``done`` 之后仍是 waiting：不写完整 Assistant Entry。"""
    async with _client(
        tmp_path,
        structured=_natural_language_scripts(),
        text={NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: [NL_SUMMARY]},
    ) as (client, _model, db):
        chat_id = await _create_conversation(client, "会话壬")
        frames = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=NL_REQUEST,
            client_request_id="request-1",
        )
        assert [name for name, _ in frames] == ["node", "message", "waiting", "done"]

        repo = ConversationRepo(db)
        run = (await repo.list_runs(chat_id))[0]
        assert run.status == "waiting"
        assert run.assistant_entry_id is None
        entries = await repo.list_entries(chat_id)
        assert [entry.payload["role"] for entry in entries] == ["user"]
        assert [
            event.event_type for event in await repo.list_run_events(run.id)
        ] == ["node", "message", "waiting", "done"]
        detail = (await client.get(f"/api/conversations/{chat_id}")).json()
        round_ = detail["rounds"][0]
        assert round_["status"] == "waiting"
        assert round_["assistants"] == []
        assert [event["event"] for event in round_["events"]] == [
            "node",
            "message",
            "waiting",
            "done",
        ]


async def test_workout_confirmation_replay_does_not_write_twice(tmp_path: Any) -> None:
    """同一来源 waiting Run 的重复确认：仍是一条训练记录与一条确认 Entry，响应同一份。"""
    async with _client(
        tmp_path,
        structured=_natural_language_scripts(),
        text={NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: [NL_SUMMARY]},
    ) as (client, _model, db):
        chat_id = await _create_conversation(client, "会话癸")
        frames = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=NL_REQUEST,
            client_request_id="request-1",
        )
        waiting = [data for name, data in frames if name == "waiting"][0]
        body = {
            "chat_id": chat_id,
            "conversation_id": THREAD_1,
            "performed_on": waiting["workout"]["performed_on"],
            "sets": waiting["workout"]["sets"],
            "plan_session_id": None,
            "auto_link": False,
        }
        first = await client.post("/api/agent/confirm-workout", json=body)
        assert first.status_code == 200
        records = build_services(
            build_repositories(db),
            db,
            SqliteHealthProbe(db, db.path.parent),
        ).records
        written = await records.list_all()
        assert len(written) == 1

        second = await client.post("/api/agent/confirm-workout", json=body)
        assert second.status_code == 200
        assert second.json() == first.json()
        assert first.json()["workout_session"]["id"] == written[0].id
        assert len(await records.list_all()) == 1

        repo = ConversationRepo(db)
        run = (await repo.list_runs(chat_id))[0]
        confirmations = await repo.read_confirmations(run.id, "workout_confirmed")
        assert len(confirmations) == 1
        assert confirmations[0].payload["workout_session_id"] == written[0].id


async def test_natural_language_record_prompts_receive_history_once(
    tmp_path: Any,
) -> None:
    """自然语言打卡的提取与摘要都带上一轮历史，本轮请求在每个载荷里只出现一次。"""
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [
                {"domain": "general", "action": "chat"},
                {
                    "domain": "workout_execution",
                    "action": "create",
                    "execution_type": "natural_language_record",
                },
            ],
            **_extraction_scripts(),
        },
        text={
            GENERAL_CHAT_SYSTEM_PROMPT: [FIRST_ANSWER],
            NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: [NL_SUMMARY],
        },
    ) as (client, model, _db):
        chat_id = await _create_conversation(client, "会话甲一")
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_2,
            request=NL_REQUEST,
            client_request_id="request-2",
        )
        history = [
            {"role": "user", "text": FIRST_REQUEST},
            {"role": "assistant", "text": FIRST_ANSWER},
        ]
        payloads = [
            json.loads(payload)
            for prompt, payload in model.calls
            if prompt
            in (NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT, NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT)
        ]
        assert len(payloads) == 2
        for payload in payloads:
            assert payload["request"] == NL_REQUEST
            assert payload["conversation_messages"] == history
            assert all(message["text"] != NL_REQUEST for message in history)


async def test_progress_harness_and_general_prompt_receive_history_once(
    tmp_path: Any,
) -> None:
    """进展工具分支与一般对话都带上历史，本轮请求在每个模型输入里只出现一次。"""
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [
                {"domain": "general", "action": "chat"},
                {"domain": "analytics", "action": "query"},
                {"domain": "general", "action": "chat"},
            ]
        },
        text={
            GENERAL_CHAT_SYSTEM_PROMPT: [FIRST_ANSWER, NL_SUMMARY],
        },
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话乙一")
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        model.harness_scripts.extend(
            [tool_call("read_progress"), final_answer(SECOND_ANSWER)]
        )
        frames = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_2,
            request=PROGRESS_REQUEST,
            client_request_id="request-2",
        )
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_3,
            request=KNOWLEDGE_REQUEST,
            client_request_id="request-3",
        )
        assert [name for name, _payload in frames] == ["node", "message", "done"]
        assert frames[0][1] == {"name": "view_progress"}
        assert frames[1][1] == {"text": SECOND_ANSWER}
        progress_messages = model.harness_calls[0].messages
        assert [type(message).__name__ for message in progress_messages] == [
            "SystemMessage",
            "HumanMessage",
            "AIMessage",
            "HumanMessage",
        ]
        assert [message.content for message in progress_messages[1:3]] == [
            FIRST_REQUEST,
            FIRST_ANSWER,
        ]
        assert progress_messages[-1].content == PROGRESS_REQUEST
        assert [
            message.content for message in progress_messages
        ].count(PROGRESS_REQUEST) == 1
        # 内部 ToolMessage 不落库：进展轮只多出 user 与 assistant 两条完整 message Entry。
        repo = ConversationRepo(db)
        runs = await repo.list_runs(chat_id)
        events = await repo.list_run_events(runs[1].id)
        assert [event.event_type for event in events] == ["node", "message", "done"]
        contents = [
            entry.payload.get("content")
            for entry in await repo.list_entries(chat_id)
            if entry.entry_type == "message"
        ]
        assert contents[:4] == [
            FIRST_REQUEST,
            FIRST_ANSWER,
            PROGRESS_REQUEST,
            SECOND_ANSWER,
        ]
        assert len(contents) == 6
        general = json.loads(
            [
                payload
                for prompt, payload in model.calls
                if prompt == GENERAL_CHAT_SYSTEM_PROMPT
            ][-1]
        )
        assert general["request"] == KNOWLEDGE_REQUEST
        assert general["conversation_messages"] == [
            {"role": "user", "text": FIRST_REQUEST},
            {"role": "assistant", "text": FIRST_ANSWER},
            {"role": "user", "text": PROGRESS_REQUEST},
            {"role": "assistant", "text": SECOND_ANSWER},
        ]
        assert all(
            message["text"] != KNOWLEDGE_REQUEST
            for message in general["conversation_messages"]
        )


async def test_planner_payload_carries_history_and_the_request_once(tmp_path: Any) -> None:
    """计划链路的 Planner 载荷带上历史，本轮请求只出现一次。"""
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [
                {"domain": "general", "action": "chat"},
                {"domain": "plan_management", "action": "create"},
            ],
            **_plan_scripts([True]),
        },
        harness=_general_harness([FIRST_ANSWER]),
    ) as (client, model, _db):
        chat_id = await _create_conversation(client, "会话丙一")
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_2,
            request=PLAN_REQUEST,
            client_request_id="request-2",
        )
        planner = model.payload_for(PLANNER_SYSTEM_PROMPT)
        assert planner["request"] == PLAN_REQUEST
        assert planner["conversation_messages"] == [
            {"role": "user", "text": FIRST_REQUEST},
            {"role": "assistant", "text": FIRST_ANSWER},
        ]


async def test_evaluator_payload_carries_history_and_the_request_once(
    tmp_path: Any,
) -> None:
    """计划链路的 Rubric 载荷同样带上历史，本轮请求只出现一次。"""
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [
                {"domain": "general", "action": "chat"},
                {"domain": "plan_management", "action": "create"},
            ],
            **_plan_scripts([True]),
        },
        harness=_general_harness([FIRST_ANSWER]),
    ) as (client, model, _db):
        chat_id = await _create_conversation(client, "会话丙二")
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_2,
            request=PLAN_REQUEST,
            client_request_id="request-2",
        )
        evaluator = model.payload_for(EVALUATOR_SYSTEM_PROMPT)
        assert set(evaluator) == {
            "request",
            "profile",
            "plan",
            "business_day",
            "conversation_messages",
        }
        assert evaluator["request"] == PLAN_REQUEST
        assert evaluator["business_day"] == BUSINESS_DAY.isoformat()
        assert evaluator["conversation_messages"] == [
            {"role": "user", "text": FIRST_REQUEST},
            {"role": "assistant", "text": FIRST_ANSWER},
        ]


async def test_blank_run_request_and_idempotency_key_return_the_standard_400(
    tmp_path: Any,
) -> None:
    """空白 request 与空白 client_request_id 在 DTO 层拒绝：不落库、不调模型、不抛 500。"""
    async with _client(tmp_path) as (client, model, db):
        chat_id = await _create_conversation(client, "会话丙三")
        before = len(model.calls)
        for body in (
            _run_body(
                chat_id=chat_id,
                thread_id=THREAD_1,
                request="",
                client_request_id="request-1",
            ),
            _run_body(
                chat_id=chat_id,
                thread_id=THREAD_1,
                request=FIRST_REQUEST,
                client_request_id="",
            ),
        ):
            response = await client.post("/api/agent/run", json=body)
            assert response.status_code == 400
            assert response.json()["error_code"] == "invalid_request"
            assert response.json()["http_status"] == 400
        assert len(model.calls) == before
        repo = ConversationRepo(db)
        assert await repo.list_runs(chat_id) == ()
        assert await repo.list_entries(chat_id) == ()


async def test_blank_conversation_title_returns_the_standard_400(tmp_path: Any) -> None:
    async with _client(tmp_path) as (client, _model, _db):
        for title in ("", "   ", "\t\n"):
            response = await client.post("/api/conversations", json={"title": title})
            assert response.status_code == 400
            assert response.json()["error_code"] == "invalid_request"
            assert response.json()["http_status"] == 400
        listed = await client.get("/api/conversations")
        assert listed.json()["conversations"] == []


async def test_run_compacts_history_and_keeps_original_entries(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 ``/api/agent/run`` 越过阈值即压缩：摘要与最近轮次共同进入下一轮，原始 Entry 保留。"""
    monkeypatch.setattr(
        run_service_module, "MODEL_CONTEXT_WINDOW_TOKENS", COMPACTION_WINDOW_TOKENS
    )
    monkeypatch.setattr(
        run_service_module, "DEFAULT_COMPACTION_SETTINGS", COMPACTION_SETTINGS
    )
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [
                {"domain": "general", "action": "chat"},
                {"domain": "general", "action": "chat"},
                {"domain": "general", "action": "chat"},
            ]
        },
        text={
            GENERAL_CHAT_SYSTEM_PROMPT: [FIRST_ANSWER, SECOND_ANSWER, THIRD_ANSWER],
            SUMMARIZATION_SYSTEM_PROMPT: [COMPACTION_SUMMARY],
        },
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话压缩甲")
        repo = ConversationRepo(db)
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        first_round_ids = {entry.id for entry in await repo.list_entries(chat_id)}
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_2,
            request=LONG_SECOND_REQUEST,
            client_request_id="request-2",
        )

        entries = await repo.list_entries(chat_id)
        compaction = await repo.read_latest_compaction(chat_id)
        assert compaction is not None
        assert set(compaction.payload) == {
            "summary",
            "first_kept_entry_id",
            "tokens_before",
        }
        assert compaction.payload["summary"] == COMPACTION_SUMMARY
        kept_user_entry = next(
            entry
            for entry in entries
            if entry.entry_type == "message"
            and entry.payload["role"] == "user"
            and entry.payload["content"] == LONG_SECOND_REQUEST
        )
        assert compaction.payload["first_kept_entry_id"] == kept_user_entry.id
        assert first_round_ids <= {entry.id for entry in entries}

        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_3,
            request=THIRD_REQUEST,
            client_request_id="request-3",
        )

        after = await repo.list_entries(chat_id)
        assert first_round_ids <= {entry.id for entry in after}
        assert [entry.entry_type for entry in after].count("compaction") == 1
        router_payloads = [
            json.loads(payload)
            for prompt, payload in model.calls
            if prompt == ROUTER_SYSTEM_PROMPT
        ]
        third = router_payloads[2]
        assert third["request"] == THIRD_REQUEST
        assert third["conversation_messages"] == [
            {
                "role": "user",
                "text": f"{COMPACTION_SUMMARY_PREFIX}{COMPACTION_SUMMARY}{COMPACTION_SUMMARY_SUFFIX}",
            },
            {"role": "user", "text": LONG_SECOND_REQUEST},
            {"role": "assistant", "text": SECOND_ANSWER},
        ]
        assert all(
            message["text"] != THIRD_REQUEST
            for message in third["conversation_messages"]
        )


async def test_compaction_failure_keeps_the_run_running_without_compaction_entry(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """摘要为空即本轮不压缩：原上下文继续进入本轮模型调用，Run 照常完成，也不落 compaction Entry。"""
    monkeypatch.setattr(
        run_service_module, "MODEL_CONTEXT_WINDOW_TOKENS", COMPACTION_WINDOW_TOKENS
    )
    monkeypatch.setattr(
        run_service_module, "DEFAULT_COMPACTION_SETTINGS", COMPACTION_SETTINGS
    )
    async with _client(
        tmp_path,
        structured={
            FitnessIntent: [
                {"domain": "general", "action": "chat"},
                {"domain": "general", "action": "chat"},
            ]
        },
        text={
            GENERAL_CHAT_SYSTEM_PROMPT: [FIRST_ANSWER, SECOND_ANSWER],
            SUMMARIZATION_SYSTEM_PROMPT: ["   \n"],
        },
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话压缩乙")
        repo = ConversationRepo(db)
        await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_1,
            request=FIRST_REQUEST,
            client_request_id="request-1",
        )
        before = {entry.id for entry in await repo.list_entries(chat_id)}

        frames = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_2,
            request=LONG_SECOND_REQUEST,
            client_request_id="request-2",
        )

        assert [name for name, _payload in frames] == ["node", "message", "done"]
        assert frames[1][1] == {"text": SECOND_ANSWER}
        # 本轮确实走到过摘要调用：失败路径不是“没触发压缩”造成的空过。
        assert SUMMARIZATION_SYSTEM_PROMPT in model.text_calls()
        runs = await repo.list_runs(chat_id)
        assert [run.status for run in runs] == ["completed", "completed"]
        assert runs[1].error_code is None
        assert await repo.read_latest_compaction(chat_id) is None
        entries = await repo.list_entries(chat_id)
        assert before <= {entry.id for entry in entries}
        assert [entry.entry_type for entry in entries] == [
            "message",
            "message",
            "message",
            "message",
        ]
        assert entries[2].payload["role"] == "user"
        assert entries[2].payload["content"] == LONG_SECOND_REQUEST
        assert entries[3].payload["role"] == "assistant"
        assert entries[3].payload["content"] == SECOND_ANSWER
        router_payloads = [
            json.loads(payload)
            for prompt, payload in model.calls
            if prompt == ROUTER_SYSTEM_PROMPT
        ]
        second = router_payloads[1]
        assert second["request"] == LONG_SECOND_REQUEST
        assert second["conversation_messages"] == [
            {"role": "user", "text": FIRST_REQUEST},
            {"role": "assistant", "text": FIRST_ANSWER},
        ]
        assert all(
            message["text"] != LONG_SECOND_REQUEST
            for message in second["conversation_messages"]
        )
