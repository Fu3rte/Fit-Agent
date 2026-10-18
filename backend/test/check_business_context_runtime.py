import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import UUID, uuid4

from app.agent.telemetry import TraceSpan
from app.agent.tools.business import business_tool_declarations
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.ai.stream import AssistantResponse
from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.profile import business_date
from app.application.business.service import BusinessService
from app.application.session import execution as http_module
from app.application.session.business_context import (
    BusinessBindings,
    collect_message_nodes,
    collect_pending_proposals,
    next_business_context_event,
)
from app.application.session.execution import execute, model_visible_entries
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.business.models import (
    BusinessContext,
    ProfileContent,
    ProfileProposalArguments,
)
from app.domain.session.models import (
    BusinessContextDelta,
    BusinessContextSnapshot,
    RegenerateCommand,
    RegenerateRequest,
    RequestContext,
    SaveConfirmation,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
    SteeringCommand,
    SteeringRequest,
    build_business_context_entry,
)
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import RunState
from app.interfaces.http_api.events import HttpRunEvents
from app.model_config import ModelConfig
from test.regression_support import temporary_root

EVIDENCE = temporary_root("business-context-runtime")
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)

PROFILE_PAYLOAD = {
    "goal": "增肌",
    "experience": None,
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}


def uid() -> str:
    return str(uuid4())


def now_ms() -> int:
    return time_ns() // 1_000_000


def system_message() -> SystemMessage:
    return SystemMessage(
        role="system",
        content="系统提示词。",
        tools_added=business_tool_declarations(),
        timestamp=1,
    )


def assistant(calls, stop_reason: str, text: str = "") -> AssistantMessage:
    content = [
        ToolCall(type="toolCall", id=call_id, name=name, arguments=arguments)
        for call_id, name, arguments in calls
    ]
    if text:
        content.append(TextContent(type="text", text=text))
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="openai",
        model="test",
        usage=USAGE,
        stop_reason=stop_reason,
        timestamp=now_ms(),
    )


def assistant_text(text: str) -> AssistantMessage:
    return assistant([], "stop", text)


def tool_result(call_id: str, name: str, text: str, *, is_error: bool = False):
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(type="text", text=text)],
        is_error=is_error,
        timestamp=now_ms(),
    )


async def build_services(path: Path):
    database = await open_database(path)
    service = SessionService(SqliteSessionRepository(database))
    repository = SqliteBusinessRepository(database)
    catalog = Catalog.load()
    business = BusinessService(
        repository, catalog, service.queries, ReplacementCoordinator()
    )
    service.attach_snapshots(business.proposals)
    async with repository.transaction():
        await repository.replace_exercises(catalog.all())
    return database, service, repository, business


async def start_run(service, session_id: str, text: str = "建档"):
    await service.create_session(session_id, "资料运行时")
    outcome = await service.accept_send(
        SendCommand(
            operation_id=uid(), session_id=session_id, request=SendRequest(text=text)
        ),
        system_message=system_message(),
    )
    return outcome.run


async def append_entry(service, session_id, run_id, parent_id, message) -> str:
    node_id = uid()
    await service.entries.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=node_id,
            parent_id=parent_id,
            run_id=run_id,
            type="message",
            messages=[message],
            created_at=now_ms(),
        )
    )
    return node_id


async def collect_bindings(business, session_id) -> BusinessBindings:
    return BusinessBindings(
        profile_confirmations=await business.profile.list_confirmation_bindings(session_id),
        workout_confirmations=await business.workouts.list_workout_confirmation_bindings(session_id),
        plan_confirmations=await business.plans.list_plan_confirmation_bindings(session_id),
        profile_displays=await business.profile.list_display_bindings(session_id),
        workout_displays=await business.workouts.list_workout_display_bindings(session_id),
        plan_displays=await business.plans.list_plan_display_bindings(session_id),
    )


async def next_event(service, business, session_id, leaf_id, request_entry_id):
    # 与 execute 内追加逻辑同口径：真实投影、白名单来源、完整分支核验与只读原因判定。
    projection = await service.queries.get_projection(session_id, leaf_id)
    visible = set(projection.context.source_entry_ids)
    nodes = collect_message_nodes(
        model_visible_entries(projection.path, visible),
        await collect_bindings(business, session_id),
    )
    proposals = collect_pending_proposals(
        await business.proposals.list_pending_references(session_id),
        {entry.id for entry in projection.path},
    )
    entry = await service.queries.get_entry(session_id, request_entry_id)
    return await next_business_context_event(
        path=projection.path,
        request=RequestContext(
            timezone="Asia/Shanghai",
            business_date=business_date(entry.created_at),
            request_entry_id=request_entry_id,
        ),
        nodes=nodes,
        proposals=proposals,
        removal_reason=business.proposals.removed_proposal_reason,
    )


async def commit(service, session_id, run_id, parent_id, details):
    node = build_business_context_entry(
        session_id=session_id,
        id=uid(),
        parent_id=parent_id,
        run_id=run_id,
        details=details,
        created_at=now_ms(),
    )
    outcome = await service.entries.append_business_context(node)
    assert outcome.created and not outcome.credential_detected
    return outcome.entry


def profile_arguments() -> ProfileProposalArguments:
    return ProfileProposalArguments(
        profile_id=1,
        base_profile_version=None,
        payload=ProfileContent(**PROFILE_PAYLOAD),
    )


async def scenario_snapshot_delta(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "snapshot.db")
    session_id = uid()
    run = await start_run(service, session_id)
    user_id = run.request_entry_id

    details = await next_event(service, business, session_id, user_id, user_id)
    assert isinstance(details, BusinessContextSnapshot), details
    assert [node.entry_id for node in details.message_nodes] == [user_id]
    assert details.message_nodes[0].role == "user"
    assert details.pending_proposals == []
    assert details.request.timezone == "Asia/Shanghai"
    request_entry = await service.queries.get_entry(session_id, user_id)
    assert details.request.business_date == business_date(request_entry.created_at)
    first = await commit(service, session_id, run.id, user_id, details)

    # 无变化：不追加事件，也不构造空增量。
    assert await next_event(service, business, session_id, first.id, user_id) is None

    assistant_id = await append_entry(
        service, session_id, run.id, first.id,
        assistant([("call-1", "prepare_profile_update", {})], "toolUse"),
    )
    context = BusinessContext(
        timezone="Asia/Shanghai",
        business_date=business_date(request_entry.created_at),
        session_id=session_id,
        run_id=run.id,
        request_entry_id=user_id,
        source_entry_id=assistant_id,
    )
    proposal = await business.profile.prepare_profile_update(context, profile_arguments())
    tool_id = await append_entry(
        service, session_id, run.id, assistant_id,
        tool_result(
            "call-1", "prepare_profile_update",
            json.dumps(proposal.model_dump(), ensure_ascii=False),
        ),
    )
    await business.profile.bind_display_entry(proposal.proposal_id, tool_id)

    delta = await next_event(service, business, session_id, tool_id, user_id)
    assert isinstance(delta, BusinessContextDelta), delta
    assert delta.request is None
    assert delta.remove_message_node_ids == []
    assert delta.remove_pending_proposals == []
    by_role = {node.role: node for node in delta.upsert_message_nodes}
    assert set(by_role) == {"assistant", "toolResult"}
    assert by_role["assistant"].entry_id == assistant_id
    assert by_role["assistant"].tool_call_ids == ["call-1"]
    assert by_role["toolResult"].entry_id == tool_id
    assert by_role["toolResult"].tool_call_id == "call-1"
    assert by_role["toolResult"].tool_name == "prepare_profile_update"
    assert by_role["toolResult"].proposal.business_kind == "profile"
    assert by_role["toolResult"].proposal.proposal_id == proposal.proposal_id
    assert len(delta.upsert_pending_proposals) == 1
    pending = delta.upsert_pending_proposals[0]
    assert pending.status == "pending"
    assert pending.display_entry_id == tool_id
    assert pending.confirmation_entry_id is None
    assert pending.request_entry_id == user_id
    assert pending.source_entry_id == assistant_id
    # 资料节点自身不进入来源清单，避免递归资料增长。
    assert first.id not in {node.entry_id for node in delta.upsert_message_nodes}
    second = await commit(service, session_id, run.id, tool_id, delta)
    assert await next_event(service, business, session_id, second.id, user_id) is None

    fixed_content = first.message.content
    history = await service.queries.get_session_history(session_id)
    assert [entry.id for entry in history.entries][-1] == second.id
    await database.close()

    # 重启恢复：同一分支得到相同追踪依据，重复读取与重启不重复追加。
    reopened = await open_database(root / "snapshot.db")
    restored_service = SessionService(SqliteSessionRepository(reopened))
    restored_business = BusinessService(
        SqliteBusinessRepository(reopened), Catalog.load(), restored_service.queries,
        ReplacementCoordinator(),
    )
    try:
        assert (
            await next_event(
                restored_service, restored_business, session_id, second.id, user_id
            )
            is None
        )
        entries = await restored_service.queries.list_entries(session_id)
        restored_first = next(entry for entry in entries if entry.id == first.id)
        assert restored_first.message.content == fixed_content
        assert restored_first.parent_id == user_id
    finally:
        await reopened.close()
    return {
        "user": user_id, "assistant": assistant_id, "tool": tool_id,
        "snapshot": first.id, "delta": second.id,
    }


async def scenario_proposal_lifecycle(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "lifecycle.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "提案")
        user_id = run.request_entry_id
        snapshot = await commit(
            service, session_id, run.id, user_id,
            await next_event(service, business, session_id, user_id, user_id),
        )
        assistant_id = await append_entry(
            service, session_id, run.id, snapshot.id,
            assistant([("call-1", "prepare_profile_update", {})], "toolUse"),
        )
        request_entry = await service.queries.get_entry(session_id, user_id)
        context = BusinessContext(
            timezone="Asia/Shanghai",
            business_date=business_date(request_entry.created_at),
            session_id=session_id,
            run_id=run.id,
            request_entry_id=user_id,
            source_entry_id=assistant_id,
        )
        first = await business.profile.prepare_profile_update(context, profile_arguments())
        upsert = await next_event(service, business, session_id, assistant_id, user_id)
        assert isinstance(upsert, BusinessContextDelta)
        assert [item.proposal_id for item in upsert.upsert_pending_proposals] == [
            first.proposal_id
        ]
        node = await commit(service, session_id, run.id, assistant_id, upsert)

        # 新提案失效旧提案：移出原因取真实状态。
        second = await business.profile.prepare_profile_update(context, profile_arguments())
        removal = await next_event(service, business, session_id, node.id, user_id)
        assert isinstance(removal, BusinessContextDelta)
        assert [item.proposal_id for item in removal.upsert_pending_proposals] == [
            second.proposal_id
        ]
        removed = {
            item.proposal_id: item.reason for item in removal.remove_pending_proposals
        }
        assert removed == {first.proposal_id: "invalidated"}, removed
        return {"invalidated": first.proposal_id, "live": second.proposal_id}
    finally:
        await database.close()


async def scenario_reason_evidence(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "evidence.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "原因")
        user_id = run.request_entry_id
        snapshot = await commit(
            service, session_id, run.id, user_id,
            await next_event(service, business, session_id, user_id, user_id),
        )
        assistant_id = await append_entry(
            service, session_id, run.id, snapshot.id,
            assistant([("call-1", "prepare_profile_update", {})], "toolUse"),
        )
        request_entry = await service.queries.get_entry(session_id, user_id)
        context = BusinessContext(
            timezone="Asia/Shanghai",
            business_date=business_date(request_entry.created_at),
            session_id=session_id,
            run_id=run.id,
            request_entry_id=user_id,
            source_entry_id=assistant_id,
        )
        pending = await business.profile.prepare_profile_update(context, profile_arguments())
        # pending 且不在当前有效集合：out_of_scope。
        assert await business.proposals.removed_proposal_reason("profile", pending.proposal_id) == "out_of_scope"
        await repository.set_snapshot_status(pending.proposal_id, "conflicted")
        assert await business.proposals.removed_proposal_reason("profile", pending.proposal_id) == "conflicted"
        await repository.set_snapshot_status(pending.proposal_id, "invalidated")
        assert await business.proposals.removed_proposal_reason("profile", pending.proposal_id) == "invalidated"
        # 绑定真实展示与确认节点后置为已保存：saved 取真实状态证据。
        tool_id = await append_entry(
            service, session_id, run.id, assistant_id,
            tool_result(
                "call-1", "prepare_profile_update",
                json.dumps(pending.model_dump(), ensure_ascii=False),
            ),
        )
        await business.profile.bind_display_entry(pending.proposal_id, tool_id)
        confirmation = await service.entries.append_confirmation_entry(
            session_id,
            SaveConfirmation(
                business_kind="profile",
                proposal_id=pending.proposal_id,
                display_entry_id=tool_id,
            ),
        )
        await repository.begin_save(pending.proposal_id, confirmation.id)
        await repository.set_snapshot_status(pending.proposal_id, "saved")
        assert await business.proposals.removed_proposal_reason("profile", pending.proposal_id) == "saved"
        # 记录实际删除且没有保存记录：deleted。
        doomed = await business.profile.prepare_profile_update(context, profile_arguments())
        await repository.delete_snapshots_for_entries(session_id, {assistant_id})
        assert await business.proposals.removed_proposal_reason("profile", doomed.proposal_id) == "deleted"
        # 未知业务类型停止报告。
        try:
            await business.proposals.removed_proposal_reason("unknown", uid())
        except ValueError:
            pass
        else:
            raise AssertionError("未知业务类型未报错")
        return {"reasons": ["out_of_scope", "conflicted", "invalidated", "saved", "deleted"]}
    finally:
        await database.close()


async def scenario_branch_isolation(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "branch.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "分支")
        user_id = run.request_entry_id
        first = await commit(
            service, session_id, run.id, user_id,
            await next_event(service, business, session_id, user_id, user_id),
        )
        await service.runs.finish_run(session_id, run.id, "completed")
        regenerated = await service.accept_regenerate(
            RegenerateCommand(
                operation_id=uid(),
                session_id=session_id,
                request=RegenerateRequest(target_entry_id=user_id),
            )
        )
        # 原基线随内容移除：新分支重新生成完整基线，不回填旧资料。
        details = await next_event(
            service, business, session_id, regenerated.run.request_entry_id, user_id
        )
        assert isinstance(details, BusinessContextSnapshot), details
        assert first.id not in [node.entry_id for node in details.message_nodes]
        assert [node.entry_id for node in details.message_nodes] == [user_id]
        return {"old": first.id}
    finally:
        await database.close()


async def scenario_steering(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "steer.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "Steering")
        user_id = run.request_entry_id
        first = await commit(
            service, session_id, run.id, user_id,
            await next_event(service, business, session_id, user_id, user_id),
        )
        coordinator = SteeringCoordinator(service)
        notified: list[dict] = []
        coordinator.open(session_id, run.id, notified.append)
        _, steering = await coordinator.accept(
            run.id,
            SteeringCommand(
                operation_id=uid(),
                session_id=session_id,
                request=SteeringRequest(target_run_id=run.id, text="补充"),
            ),
        )
        consumed = await coordinator.consume(run.id, await coordinator.take(run.id))
        assert consumed is not None
        assert notified[0]["status"] == "consumed"
        new_user = await service.queries.get_entry(session_id, consumed)
        delta = await next_event(service, business, session_id, consumed, consumed)
        assert isinstance(delta, BusinessContextDelta), delta
        assert delta.request is not None
        assert delta.request.request_entry_id == consumed
        assert delta.request.business_date == business_date(new_user.created_at)
        assert [node.entry_id for node in delta.upsert_message_nodes] == [consumed]
        assert delta.remove_message_node_ids == []
        return {"request_entry_id": consumed, "baseline": first.id}
    finally:
        await database.close()


def scripted_stream(turns, captured):
    pending = list(turns)

    def stream_fn(model, context, options):
        captured.append(context["messages"])

        async def source():
            assert pending, "脚本化模型流已用尽"
            message = pending.pop(0)
            partial = message.model_copy(
                update={"content": [], "stop_reason": "pending", "usage": None}
            )
            yield {"type": "start", "partial": partial}
            yield {"type": "done", "reason": message.stop_reason, "message": message}

        return AssistantResponse(source())

    return stream_fn


def drain(state: RunState) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    while not state.events.empty():
        event = state.events.get_nowait()
        events.append((event.event, event.data))
    return events


async def scenario_execute(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "execute.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "多工具批次")
        state = RunState(UUID(session_id))
        state.run_id = UUID(run.id)
        state.operation_id = uid()
        state.request_entry_id = run.request_entry_id
        state.secrets = ("test-secret",)
        coordinator = SteeringCoordinator(service)
        state.coordinator = coordinator
        coordinator.open(
            session_id, run.id, lambda data: state.publish("steering_status", data)
        )
        projection = await service.queries.get_projection(session_id, run.request_entry_id)
        loop = asyncio.get_running_loop()
        model = ModelConfig(
            api="openai-completions",
            base_url="https://example.com/v1",
            model="test-model",
            provider="openai",
            api_key="test-secret",
        )
        turns = [
            assistant(
                [
                    ("read-1", "get_profile", {}),
                    (
                        "prepare-1",
                        "prepare_profile_update",
                        {
                            "profile_id": 1,
                            "base_profile_version": None,
                            "payload": dict(PROFILE_PAYLOAD),
                        },
                    ),
                ],
                "toolUse",
            ),
            assistant_text("已整理完整画像，等待确认。"),
        ]
        captured: list = []
        stream_fn = scripted_stream(turns, captured)
        real = http_module.run_agent_loop

        def patched(prompts, context, config, emit, signal=None):
            return real(prompts, context, config, emit, signal, stream_fn=stream_fn)

        http_module.run_agent_loop = patched
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    execute, run, session_id, projection.context.messages, {},
                    state, service.queries, service.entries, business, loop, model, state.secrets, TraceSpan(),
                    create_events=lambda guard, parents: HttpRunEvents(
                        state.publish, guard, parents, TraceSpan().record_first_text
                    ),
                )
                stop = await asyncio.wrap_future(future)
        finally:
            http_module.run_agent_loop = real

        assert stop == "stop"
        events = drain(state)
        names = [name for name, _ in events]
        # 全部工具结果与展示绑定先于该批资料通知；资料通知先于下一次模型消息事件。
        assert names == [
            "custom_message",
            "message_start",
            "message_end",
            "tool_start",
            "tool_execution_end",
            "tool_start",
            "tool_execution_end",
            "tool_result",
            "tool_result",
            "custom_message",
            "message_start",
            "message_end",
        ], names

        first = events[0][1]
        second = events[9][1]
        assert set(first) == {"type", "entry_id", "parent_id", "run_id", "created_at"}
        assert first["type"] == "custom_message"
        assert first["parent_id"] == run.request_entry_id
        assert first["run_id"] == run.id
        tool_entries = [data["entry_id"] for name, data in events if name == "tool_result"]
        assert second["parent_id"] == tool_entries[-1]
        assert second["run_id"] == run.id
        # 后续助手节点 parent_id 指向最新资料节点。
        assistants = [
            data for name, data in events
            if name == "message_end" and data.get("stop_reason") == "stop"
        ]
        assert assistants[-1]["parent_id"] == second["entry_id"]
        # 资料身份通知不泄露业务资料、模型文本或内部字段。
        raw = json.dumps(
            [data for name, data in events if name == "custom_message"],
            ensure_ascii=False,
        )
        for leaked in ("goal", "增肌", "details", "display", "content", "<business_context>"):
            assert leaked not in raw, leaked

        history = await service.queries.get_session_history(session_id)
        custom = [entry for entry in history.entries if entry.type == "custom_message"]
        assert [entry.id for entry in custom] == [first["entry_id"], second["entry_id"]]
        assert custom[0].parent_id == run.request_entry_id
        assert isinstance(custom[0].message.details, BusinessContextSnapshot)
        delta = custom[1].message.details
        assert isinstance(delta, BusinessContextDelta)
        assert len(delta.upsert_pending_proposals) == 1
        assert delta.upsert_pending_proposals[0].display_entry_id == tool_entries[-1]
        assert len(delta.upsert_message_nodes) == 3
        return {"events": names, "snapshot": first["entry_id"], "delta": second["entry_id"]}
    finally:
        await database.close()


async def scenario_tool_failure(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "failure.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "工具失败")
        state = RunState(UUID(session_id))
        state.run_id = UUID(run.id)
        state.operation_id = uid()
        state.request_entry_id = run.request_entry_id
        state.secrets = ()
        coordinator = SteeringCoordinator(service)
        state.coordinator = coordinator
        coordinator.open(session_id, run.id, lambda data: None)
        projection = await service.queries.get_projection(session_id, run.request_entry_id)
        loop = asyncio.get_running_loop()
        model = ModelConfig(
            api="openai-completions", base_url="https://example.com/v1",
            model="test-model", provider="openai", api_key="test-secret",
        )
        turns = [
            assistant([("bad-1", "prepare_profile_update", {"profile_id": 2})], "toolUse"),
            assistant_text("参数不合法，已停止。"),
        ]
        stream_fn = scripted_stream(turns, [])
        real = http_module.run_agent_loop

        def patched(prompts, context, config, emit, signal=None):
            return real(prompts, context, config, emit, signal, stream_fn=stream_fn)

        http_module.run_agent_loop = patched
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    execute, run, session_id, projection.context.messages, {},
                    state, service.queries, service.entries, business, loop, model, state.secrets, TraceSpan(),
                    create_events=lambda guard, parents: HttpRunEvents(
                        state.publish, guard, parents, TraceSpan().record_first_text
                    ),
                )
                stop = await asyncio.wrap_future(future)
        finally:
            http_module.run_agent_loop = real
        assert stop == "stop"
        events = drain(state)
        names = [name for name, _ in events]
        assert names.count("custom_message") == 2, names
        # 工具调用与全部结果之间不得插入资料节点。
        last_custom = len(names) - 1 - names[::-1].index("custom_message")
        assert not any(
            name == "custom_message" for name in names[names.index("message_end") + 1:last_custom]
        ), names
        assert names.index("tool_result") < last_custom
        # 失败结果仍作为来源节点进入资料增量。
        assert "tool_result" in names
        history = await service.queries.get_session_history(session_id)
        custom = [entry for entry in history.entries if entry.type == "custom_message"]
        assert custom[1].message.details.mode == "delta"
        upsert = custom[1].message.details.upsert_message_nodes
        assert any(node.role == "toolResult" for node in upsert)
        assert custom[1].message.details.remove_pending_proposals == []
        return {"events": names}
    finally:
        await database.close()


def user_request_texts(messages: list) -> list[str]:
    # 已进入模型请求的 user-role 文本：真实用户输入与内部资料投影共用同一角色。
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, UserMessage):
            continue
        if isinstance(message.content, str):
            texts.append(message.content)
        else:
            texts.append(
                "".join(
                    block.text for block in message.content
                    if isinstance(block, TextContent)
                )
            )
    return texts


async def scenario_steering_and_batch(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "steer-batch.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id, "批次中补充")
        state = RunState(UUID(session_id))
        state.run_id = UUID(run.id)
        state.operation_id = uid()
        state.request_entry_id = run.request_entry_id
        state.secrets = ("test-secret",)
        coordinator = SteeringCoordinator(service)
        state.coordinator = coordinator
        coordinator.open(
            session_id, run.id, lambda data: state.publish("steering_status", data)
        )
        projection = await service.queries.get_projection(session_id, run.request_entry_id)
        loop = asyncio.get_running_loop()
        model = ModelConfig(
            api="openai-completions", base_url="https://example.com/v1",
            model="test-model", provider="openai", api_key="test-secret",
        )
        turns = [
            assistant(
                [
                    ("read-1", "get_profile", {}),
                    (
                        "prepare-1",
                        "prepare_profile_update",
                        {
                            "profile_id": 1,
                            "base_profile_version": None,
                            "payload": dict(PROFILE_PAYLOAD),
                        },
                    ),
                ],
                "toolUse",
            ),
            assistant_text("完成。"),
        ]
        captured: list = []
        pending_turns = list(turns)

        def stream_fn(model_spec, context, options):
            captured.append(context["messages"])

            async def source():
                message = pending_turns.pop(0)
                if len(captured) == 1:
                    # 运行中补充在本批工具结果之前受理：消费仍发生在本批结果提交与通知之后。
                    asyncio.run_coroutine_threadsafe(
                        coordinator.accept(
                            run.id,
                            SteeringCommand(
                                operation_id=uid(),
                                session_id=session_id,
                                request=SteeringRequest(
                                    target_run_id=run.id, text="补充一句"
                                ),
                            ),
                        ),
                        loop,
                    ).result()
                partial = message.model_copy(
                    update={"content": [], "stop_reason": "pending", "usage": None}
                )
                yield {"type": "start", "partial": partial}
                yield {"type": "done", "reason": message.stop_reason, "message": message}

            return AssistantResponse(source())

        real = http_module.run_agent_loop

        def patched(prompts, context, config, emit, signal=None):
            return real(prompts, context, config, emit, signal, stream_fn=stream_fn)

        http_module.run_agent_loop = patched
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    execute, run, session_id, projection.context.messages, {},
                    state, service.queries, service.entries, business, loop, model, state.secrets, TraceSpan(),
                    create_events=lambda guard, parents: HttpRunEvents(
                        state.publish, guard, parents, TraceSpan().record_first_text
                    ),
                )
                stop = await asyncio.wrap_future(future)
        finally:
            http_module.run_agent_loop = real

        assert stop == "stop"
        events = drain(state)
        names = [name for name, _ in events]
        # 本批全部工具结果先提交与通知，随后消费通知，再是该批资料身份通知。
        assert names == [
            "custom_message",
            "message_start",
            "message_end",
            "tool_start",
            "tool_execution_end",
            "tool_start",
            "tool_execution_end",
            "tool_result",
            "tool_result",
            "steering_status",
            "custom_message",
            "message_start",
            "message_end",
        ], names
        baseline, steering, delta_event = events[0][1], events[9][1], events[10][1]
        assert steering["status"] == "consumed" and steering["entry_id"]
        assert set(baseline) == set(delta_event) == {
            "type", "entry_id", "parent_id", "run_id", "created_at"
        }
        assert baseline["parent_id"] == run.request_entry_id
        assert delta_event["parent_id"] == steering["entry_id"]
        # 两条结果各有独立身份。
        results = [data for name, data in events if name == "tool_result"]
        assert [data["tool_call_id"] for data in results] == ["read-1", "prepare-1"]
        assert len({data["entry_id"] for data in results}) == 2
        # 消费节点挂在本批最后一个工具结果之后，资料节点再挂在该真实用户节点之后。
        last_result = results[-1]["entry_id"]
        consumed = await service.queries.get_entry(session_id, steering["entry_id"])
        assert consumed.parent_id == last_result
        assistants = [
            data for name, data in events
            if name == "message_end" and data.get("stop_reason") == "stop"
        ]
        assert assistants[-1]["parent_id"] == delta_event["entry_id"]

        history = await service.queries.get_session_history(session_id)
        custom = [entry for entry in history.entries if entry.type == "custom_message"]
        delta = custom[1].message.details
        assert isinstance(delta, BusinessContextDelta), delta
        assert delta.request is not None
        assert delta.request.request_entry_id == steering["entry_id"]
        assert delta.request.business_date == business_date(consumed.created_at)
        assert steering["entry_id"] in [
            node.entry_id for node in delta.upsert_message_nodes
        ]
        assert delta.remove_message_node_ids == []
        # 批内展示绑定随结果提交完成后才进入增量：提案绑定到本批最后一个结果节点。
        assert len(delta.upsert_pending_proposals) == 1
        assert delta.upsert_pending_proposals[0].display_entry_id == last_result

        # 每条资料只进入对应请求一次；消费后的用户输入在请求中只合并一次。
        assert len(captured) == 2
        for index, expected in ((0, 1), (1, 2)):
            texts = user_request_texts(captured[index])
            assert sum("<business_context>" in text for text in texts) == expected
        assert sum("补充一句" in text for text in user_request_texts(captured[1])) == 1
        assert sum("补充一句" in text for text in user_request_texts(captured[0])) == 0
        return {
            "events": names,
            "baseline": baseline["entry_id"],
            "results": [data["entry_id"] for data in results],
            "consumed": steering["entry_id"],
            "delta": delta_event["entry_id"],
        }
    finally:
        await database.close()


async def run(root: Path) -> dict:
    return {
        "snapshot_delta": await scenario_snapshot_delta(root),
        "proposal_lifecycle": await scenario_proposal_lifecycle(root),
        "reason_evidence": await scenario_reason_evidence(root),
        "branch": await scenario_branch_isolation(root),
        "steering": await scenario_steering(root),
        "steering_and_batch": await scenario_steering_and_batch(root),
        "execute": await scenario_execute(root),
        "tool_failure": await scenario_tool_failure(root),
    }


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        result = asyncio.run(run(Path(directory)))
    (EVIDENCE / "business-context-runtime.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 首次基线、后续增量、无变化不追加、重启后比较依据一致、分支隔离、"
        "提案增删与移出原因、Steering 更新请求身份、多工具批次结果与绑定完成后追加、"
        "多工具批次全部结果提交与通知完成后才消费运行中补充、追加位置与通知顺序、"
        "工具失败安全边界、资料节点 SSE 身份通知顺序与五字段白名单"
    )


if __name__ == "__main__":
    check()
