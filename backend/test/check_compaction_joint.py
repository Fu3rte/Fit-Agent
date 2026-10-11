import asyncio
import json
import time
from pathlib import Path

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools.business import business_tool_declarations
from app.agent.tools.files import create_file_tools
from app.ai.messages import SystemMessage, UserMessage
from app.ai.model_capabilities import resolve_model_spec
from app.domain.session.models import (
    CompactionEntry,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
)
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import active, app
from app.model_config import load_model_config
from test.check_http import events, validate_events, wait_idle
from test.check_profile_confirmation import (
    PREPARE,
    Fixture,
    assistant_prepare_call,
    envelope,
    message_text,
    new_id,
    now_ms,
    payload,
    prepare_arguments,
)
from test.regression_support import (
    Server,
    client,
    install_test_model_config,
    patch_default_database,
)

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-joint-acceptance"
FILLER = "the quick brown fox jumps over the lazy dog and keeps training hard. "
# 20k token 级 filler 节点：生产保留预算 20000 决定切点，多节点保证切点可推进。
PART_CHARS = 80_000
PARTS = 6
EXTRA_PARTS = 5


def part_text() -> str:
    return (FILLER * (PART_CHARS // len(FILLER) + 1))[:PART_CHARS]


def system_message(session_id: str) -> SystemMessage:
    tools = create_file_tools(session_id)
    return SystemMessage(
        role="system",
        content=SYSTEM_PROMPT,
        tools_added=[tool.definition() for tool in tools.values()] + business_tool_declarations(),
        timestamp=time.time_ns() // 1_000_000,
    )


async def seed(path: Path, session_id: str, parts: int) -> dict:
    # 真实隔离 SQLite：真实准备工具产生待确认画像快照，长 filler 历史触发生产阈值压缩。
    fixture = await Fixture(path).seeded()
    ids: dict = {"session": session_id}
    try:
        created, _ = await fixture.service.create_session_result(session_id, "联合验收")
        assert created
        system = system_message(session_id)
        outcome = await fixture.service.accept_send(
            SendCommand(
                operation_id=new_id(),
                session_id=session_id,
                request=SendRequest(text="请根据我的描述整理一份完整画像并展示，等待我确认。"),
            ),
            system_message=system,
        )
        run = outcome.run
        request_id = run.request_entry_id
        call_id = new_id()
        arguments = prepare_arguments(None, payload())
        source = await fixture.append(
            session_id, run.id, request_id, assistant_prepare_call(call_id, arguments, now_ms())
        )
        prepared: dict[str, str] = {}
        message = await fixture.invoke(
            PREPARE, arguments, fixture.context(session_id, request_id, source), prepared,
            call_id=call_id,
        )
        assert message.is_error is False, message_text(message)
        proposal = envelope(message)["proposal_id"]
        display = await fixture.append(session_id, run.id, source, message)
        await fixture.business.bind_display_entry(proposal, display)
        # 长历史 filler 用户节点直接经 repository 写入：append_entry 只接受助手/工具结果节点。
        sessions = SqliteSessionRepository(fixture.database)
        filler_ids = []
        parent = display
        async with sessions.transaction():
            for _ in range(parts):
                node_id = new_id()
                await sessions.insert_entry(SessionMessageEntry(
                    session_id=session_id, id=node_id, parent_id=parent, run_id=None,
                    type="message",
                    messages=[UserMessage(role="user", content=part_text(), timestamp=now_ms())],
                    created_at=now_ms(),
                ))
                filler_ids.append(node_id)
                parent = node_id
            stored = await sessions.get_session(session_id)
            await sessions.update_session(
                stored.model_copy(update={"active_leaf_id": parent, "updated_at": now_ms()})
            )
        await fixture.service.finish_run(session_id, run.id, "completed")
        ids.update(
            request=request_id, source=source, call=call_id, display=display,
            proposal=proposal, fillers=filler_ids, leaf=parent,
        )
        return ids
    finally:
        await fixture.close()


def turn(http, session_id: str, request: str):
    body = {"session_id": session_id, "operation_id": new_id(), "request": request}
    with http.stream("POST", "/api/agent/run", json=body) as response:
        assert response.status_code == 200, response.text
        request_id = response.headers["X-Request-Entry-ID"]
        result = list(events(response))
    wait_idle()
    validate_events([e for e in result if e["event"] not in {"compaction_start", "compaction_end"}])
    return request_id, result


def outputs_of(result: list[dict]) -> list[dict]:
    names = {
        event["data"]["tool_call_id"]: event["data"]
        for event in result
        if event["event"] == "tool_start"
    }
    return [
        {
            **event["data"],
            "name": names[event["data"]["tool_call_id"]]["name"],
            "arguments": names[event["data"]["tool_call_id"]]["arguments"],
        }
        for event in result
        if event["event"] == "tool_result"
    ]


def call(coro, loop):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def json_content(output: dict) -> dict:
    return json.loads(output["content"])


def compaction_events(result: list[dict]) -> tuple[list[dict], list[dict]]:
    return (
        [e for e in result if e["event"] == "compaction_start"],
        [e for e in result if e["event"] == "compaction_end"],
    )


def scenario_read_and_save(http, ids: dict) -> dict:
    session_id = ids["session"]
    service = app.state.session_service
    loop = app.state.loop

    # 回合一：生产阈值触发压缩，展示节点进入总结范围；模型经 message_nodes 引用读取完整 payload。
    _, result = turn(
        http, session_id,
        "系统上下文 message_nodes 里存在 role=pendingProposal 的引用。"
        "调用 get_pending_proposal：business_kind 用该引用的 business_kind，"
        "proposal_id 用该引用的 proposal_id，读取完整待确认画像，"
        "然后用一句话报告 payload.goal 的值。",
    )
    starts, ends = compaction_events(result)
    assert len(starts) == 1 and starts[0]["data"]["reason"] == "threshold", [e["event"] for e in result]
    assert len(ends) == 1 and ends[0]["data"]["entry_id"] == starts[0]["data"]["compaction_id"]
    pending_reads = [
        o for o in outputs_of(result)
        if o["name"] == "get_pending_proposal" and not o["is_error"]
    ]
    assert len(pending_reads) == 1, outputs_of(result)
    assert pending_reads[0]["arguments"] == {
        "business_kind": "profile", "proposal_id": ids["proposal"],
    }
    body = json_content(pending_reads[0])
    assert body["proposal_id"] == ids["proposal"] and body["status"] == "pending"
    assert body["display_entry_id"] == ids["display"]
    assert body["request_entry_id"] == ids["request"] and body["source_entry_id"] == ids["source"]
    assert body["confirmation_entry_id"] is None
    assert body["payload"] == payload()

    # 展示节点已被总结：模型投影来源不再包含展示/来源节点，但原始路径仍保留。
    run = call(service.list_runs(session_id), loop)[-1]
    projection = call(service.get_projection(session_id, run.last_entry_id), loop)
    sources = [str(item) for item in projection.context.source_entry_ids]
    assert ids["display"] not in sources and ids["source"] not in sources
    checkpoints = [
        e for e in call(service.list_entries(session_id), loop) if isinstance(e, CompactionEntry)
    ]
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert str(checkpoint.first_kept_entry_id) == ids["fillers"][-1]
    assert checkpoint.usage.total_tokens > 0 and checkpoint.summary.strip()

    read_evidence = {
        "checkpoint_id": checkpoint.id,
        "first_kept_entry_id": checkpoint.first_kept_entry_id,
        "tokens_before": checkpoint.tokens_before,
        "summary_usage": checkpoint.usage.model_dump(),
        "summary_chars": len(checkpoint.summary),
        "reference_proposal": ids["proposal"],
        "display_summarized": ids["display"] not in sources,
        "read_payload_goal": body["payload"]["goal"],
    }

    # 回合二：真实模型以真实引用保存；授权来自真实用户确认节点。
    confirmation_id, result2 = turn(
        http, session_id,
        "我确认保存。调用 save_profile_update：proposal_id 与 display_entry_id 取 message_nodes 中 "
        "pendingProposal 引用的值，confirmation_entry_id 取 business_context 的 request_entry_id。",
    )
    saved = [
        o for o in outputs_of(result2)
        if o["name"] == "save_profile_update" and not o["is_error"]
    ]
    assert len(saved) == 1, outputs_of(result2)
    result_body = json_content(saved[0])
    assert result_body["proposal_id"] == ids["proposal"]
    assert result_body["version"] == 1 and result_body["content"] == payload()
    profile = call(app.state.business.get_profile(), loop)
    assert profile.version == 1 and profile.content.model_dump() == payload()
    run2 = call(service.list_runs(session_id), loop)[-1]
    assert run2.status == "completed" and str(run2.request_entry_id) == confirmation_id
    return {
        "read": read_evidence,
        "save": {
            "confirmation_entry_id": confirmation_id,
            "saved": result_body,
            "profile_version": profile.version,
        },
    }


def append_part(session_id: str, text: str) -> str:
    # 服务端直调：向当前会话追加真实用户节点并推进叶子，供第二次阈值压缩消费。
    service = app.state.session_service
    loop = app.state.loop
    sessions = service._repository

    async def run() -> str:
        stored = await sessions.get_session(session_id)
        node_id = new_id()
        async with sessions.transaction():
            await sessions.insert_entry(SessionMessageEntry(
                session_id=session_id, id=node_id, parent_id=stored.active_leaf_id,
                run_id=None, type="message",
                messages=[UserMessage(role="user", content=text, timestamp=now_ms())],
                created_at=now_ms(),
            ))
            await sessions.update_session(
                stored.model_copy(update={"active_leaf_id": node_id, "updated_at": now_ms()})
            )
        return node_id

    return call(run(), loop)


def scenario_repeated_compaction(http, ids: dict) -> dict:
    session_id = ids["session"]
    service = app.state.session_service
    loop = app.state.loop
    before = [
        e for e in call(service.list_entries(session_id), loop) if isinstance(e, CompactionEntry)
    ]
    appended = [append_part(session_id, part_text()) for _ in range(EXTRA_PARTS)]
    _, result = turn(http, session_id, "只回复：继续。")
    starts, ends = compaction_events(result)
    reasons = [item["data"]["reason"] for item in starts]
    assert reasons and reasons[0] == "threshold" and len(ends) == len(starts), reasons
    after = [
        e for e in call(service.list_entries(session_id), loop) if isinstance(e, CompactionEntry)
    ]
    assert len(after) > len(before), (len(before), len(after))
    checkpoint = after[-1]
    assert str(checkpoint.first_kept_entry_id) != str(before[-1].first_kept_entry_id)
    assert checkpoint.summary.strip() and checkpoint.usage.total_tokens > 0
    return {
        "compaction_count_before": len(before),
        "compaction_count_after": len(after),
        "reasons": reasons,
        "checkpoint_id": checkpoint.id,
        "first_kept_entry_id": checkpoint.first_kept_entry_id,
        "previous_first_kept": before[-1].first_kept_entry_id,
        "tokens_before": checkpoint.tokens_before,
        "summary_usage": checkpoint.usage.model_dump(),
        "appended_fillers": appended,
    }


def scenario_rejections(ids: dict) -> dict:
    from app.agent.tool import run_tool_batch
    from app.agent.tools.business import bind_business_tools
    from app.ai.messages import ToolCall
    from app.domain.business.models import BusinessContext

    business = app.state.business
    service = app.state.session_service
    loop = app.state.loop
    declared = {item.name: item for item in business_tool_declarations()}

    async def build_context(session_id: str):
        entries = await service.list_entries(session_id)
        request = next(
            (e.id for e in entries if e.messages and e.messages[0].role == "user"), new_id()
        )
        return BusinessContext(
            timezone="Asia/Shanghai", business_date="2026-01-01",
            session_id=session_id, run_id=new_id(),
            request_entry_id=request, source_entry_id=request,
        )

    def code_for(business_kind: str, proposal_id: str, session_id: str) -> str:
        context = call(build_context(session_id), loop)

        async def run() -> str:
            tools = bind_business_tools(business, context, lambda c: call(c, loop), {}, {}, {})
            execution = await run_tool_batch(
                [ToolCall(
                    type="toolCall", id=new_id(), name="get_pending_proposal",
                    arguments={"business_kind": business_kind, "proposal_id": proposal_id},
                )],
                tools={"get_pending_proposal": tools["get_pending_proposal"]},
                declared=declared,
            )
            message = execution.messages[0]
            assert message.is_error, message_text(message)
            text = message_text(message)
            try:
                return json.loads(text)["code"]
            except (json.JSONDecodeError, KeyError, TypeError):
                return "invalid_arguments" if "参数校验失败" in text else text[:60]

        return call(run(), loop)

    other_session = call(service.create_session_result(new_id(), "另一会话"), loop)
    other_id = other_session[1].id
    return {
        "illegal_kind": code_for("session", ids["proposal"], ids["session"]),
        "unknown_proposal": code_for("profile", new_id(), ids["session"]),
        "cross_session": code_for("profile", ids["proposal"], other_id),
    }


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    install_test_model_config()
    config = load_model_config()
    spec = resolve_model_spec(config)
    path = patch_default_database("compaction-joint")
    session_id = new_id()
    ids = asyncio.run(seed(path, session_id, PARTS))
    evidence: dict = {
        "model": {
            "id": spec.id, "provider": spec.provider,
            "context_window": spec.context_window, "max_tokens": spec.max_tokens,
        },
        "session": session_id,
        "ids": {key: value for key, value in ids.items() if key != "fillers"},
    }
    with Server(app) as server, client(server.base_url) as http:
        created = http.post("/api/sessions", json={"session_id": session_id, "title": "联合验收"})
        assert created.status_code in {200, 201}, created.text
        evidence["read_and_save"] = scenario_read_and_save(http, ids)
        evidence["repeated_compaction"] = scenario_repeated_compaction(http, ids)
        evidence["rejections"] = scenario_rejections(ids)
    assert not active.locked()
    (ROOT / "joint-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("PASS: 真实阈值压缩、get_pending_proposal 完整读取、真实模型确认保存、重复压缩、引用拒绝")


if __name__ == "__main__":
    check()
