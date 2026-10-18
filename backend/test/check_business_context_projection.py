import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from app.agent import compaction as C
from app.agent.compaction_summary import (
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    CompactionSummaryMessage,
)
from app.agent.message_context import (
    AgentMessage,
    business_context_to_llm,
    convert_to_llm,
)
from app.agent.telemetry import TraceSpan
from app.agent.tools.business import business_tool_declarations
from app.ai.api import anthropic_messages, openai_completions
from app.ai.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from app.ai.stream import AssistantResponse
from app.ai.types import ModelSpec
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
from app.domain.session.models import (
    BusinessContextDelta,
    BusinessContextEntry,
    BusinessContextMessage,
    BusinessContextSnapshot,
    RequestContext,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
    build_business_context_entry,
    build_business_context_message,
    business_context_content,
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

EVIDENCE = temporary_root("business-context-projection")
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
BUSINESS_DATE = "2026-10-12"
ADAPTER = TypeAdapter(Message)
OPENAI = ModelSpec(
    api="openai-completions",
    provider="test",
    id="gpt-test",
    base_url="https://example.test",
    context_window=128000,
    max_tokens=16384,
)
ANTHROPIC = ModelSpec(
    api="anthropic-messages",
    provider="test",
    id="claude-test",
    base_url="https://example.test",
    context_window=128000,
    max_tokens=16384,
)
SYSTEM_TEXT = "基础系统提示。"


def uid() -> str:
    return str(uuid4())


def now_ms() -> int:
    return time_ns() // 1_000_000


def system_message() -> SystemMessage:
    return SystemMessage(
        role="system",
        content=SYSTEM_TEXT,
        tools_added=business_tool_declarations(),
        timestamp=1,
    )


def snapshot_details(request_entry_id: str) -> BusinessContextSnapshot:
    return BusinessContextSnapshot(
        mode="snapshot",
        request=RequestContext(
            timezone="Asia/Shanghai",
            business_date=BUSINESS_DATE,
            request_entry_id=request_entry_id,
        ),
        message_nodes=[
            {
                "entry_id": request_entry_id,
                "role": "user",
                "confirmation": None,
                "proposal": None,
            }
        ],
        pending_proposals=[],
    )


def assistant(calls, stop_reason: str, text: str = "", usage: Usage | None = USAGE) -> AssistantMessage:
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
        usage=usage,
        stop_reason=stop_reason,
        timestamp=now_ms(),
    )


def assistant_text(text: str) -> AssistantMessage:
    return assistant([], "stop", text)


def tool_result(call_id: str, name: str, text: str) -> ToolResultMessage:
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(type="text", text=text)],
        is_error=False,
        timestamp=now_ms(),
    )


def message_projection(messages) -> list[dict]:
    return [message.model_dump(exclude_unset=True) for message in messages]


def business_user_text(message: Message) -> str:
    assert isinstance(message, UserMessage)
    assert not isinstance(message.content, str)
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


async def build_services(path: Path):
    database = await open_database(path)
    service = SessionService(SqliteSessionRepository(database))
    repository = SqliteBusinessRepository(database)
    catalog = Catalog.load()
    business = BusinessService(repository, catalog, service.queries, ReplacementCoordinator())
    service.attach_snapshots(business.proposals)
    async with repository.transaction():
        await repository.replace_exercises(catalog.all())
    return database, service, repository, business


async def start_run(service, session_id: str, text: str = "建档"):
    await service.create_session(session_id, "资料投影")
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


async def next_event(service, business, session_id, leaf_id, request_entry_id):
    projection = await service.queries.get_projection(session_id, leaf_id)
    visible = set(projection.context.source_entry_ids)
    nodes = collect_message_nodes(
        model_visible_entries(projection.path, visible),
        BusinessBindings(
            profile_confirmations=await business.profile.list_confirmation_bindings(session_id),
            workout_confirmations=await business.workouts.list_workout_confirmation_bindings(session_id),
            plan_confirmations=await business.plans.list_plan_confirmation_bindings(session_id),
            profile_displays=await business.profile.list_display_bindings(session_id),
            workout_displays=await business.workouts.list_workout_display_bindings(session_id),
            plan_displays=await business.plans.list_plan_display_bindings(session_id),
        ),
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


def check_conversion() -> None:
    user_id = uid()
    created = 1786766400000
    business = build_business_context_message(
        details=snapshot_details(user_id), created_at=created
    )
    assert isinstance(business, BusinessContextMessage)
    assert business.display is False
    summary = CompactionSummaryMessage(
        role="compactionSummary", summary="历史摘要", tokens_before=88, timestamp=7
    )
    sources = [
        system_message(),
        UserMessage(role="user", content="问题", timestamp=2),
        business,
        summary,
        assistant_text("完成"),
    ]
    before = json.dumps(
        [message.model_dump(exclude_unset=True) for message in sources],
        ensure_ascii=False,
    )
    identities = [id(message) for message in sources]

    converted = convert_to_llm(sources)
    # 内部资料与原位置：资料转换为 user 消息，摘要保持既有转换，原顺序不变。
    assert [message.role for message in converted] == [
        "system",
        "user",
        "user",
        "user",
        "assistant",
    ]
    data = converted[2]
    assert isinstance(data, UserMessage)
    assert business_user_text(data) == business_context_content(business.details)
    assert data.timestamp == created == business.timestamp
    # custom_type / display / details 不作为额外协议字段。
    assert set(data.model_dump(exclude_unset=True)) == {"role", "content", "timestamp"}
    assert "business_context" not in data.model_dump(exclude_unset=True)
    # 严格校验：转换为标准 Message 联合成员。
    assert isinstance(ADAPTER.validate_python(data.model_dump(exclude_unset=True), strict=True), UserMessage)
    # 摘要转换保持完整。
    assert business_user_text(converted[3]) == (
        COMPACTION_SUMMARY_PREFIX + "历史摘要" + COMPACTION_SUMMARY_SUFFIX
    )
    assert converted[3].timestamp == 7

    # 输入对象隔离：转换不改写输入，也返回独立对象。
    assert json.dumps(
        [message.model_dump(exclude_unset=True) for message in sources],
        ensure_ascii=False,
    ) == before
    assert [id(message) for message in sources] == identities
    assert data is not business
    data.content[0].text = "改写"
    assert business.content == business_context_content(business.details)

    # 服务商 Message 联合保持既有协议类型：内部资料消息不被接受。
    try:
        ADAPTER.validate_python(business.model_dump(exclude_unset=True), strict=True)
    except ValidationError:
        pass
    else:
        raise AssertionError("服务商 Message 联合接受了内部资料消息")
    agent_adapter = TypeAdapter(AgentMessage)
    assert isinstance(
        agent_adapter.validate_python(business.model_dump(exclude_unset=True), strict=True),
        BusinessContextMessage,
    )


async def scenario_projection(root: Path) -> dict:
    database, service, repository, business = await build_services(root / "projection.db")
    try:
        session_id = uid()
        run = await start_run(service, session_id)
        user_id = run.request_entry_id
        snapshot = await commit(
            service, session_id, run.id, user_id,
            await next_event(service, business, session_id, user_id, user_id),
        )
        assistant_id = await append_entry(
            service, session_id, run.id, snapshot.id,
            assistant(
                [("call-1", "get_profile", {}), ("call-2", "get_current_plan", {})],
                "toolUse",
            ),
        )
        first = await append_entry(
            service, session_id, run.id, assistant_id, tool_result("call-1", "get_profile", "{}")
        )
        second = await append_entry(
            service, session_id, run.id, first,
            tool_result("call-2", "get_current_plan", "{}"),
        )
        delta = await commit(
            service, session_id, run.id, second,
            await next_event(service, business, session_id, second, user_id),
        )
        assert isinstance(delta.message.details, BusinessContextDelta)

        projection = await service.queries.get_projection(session_id, delta.id)
        by_id = {entry.id: entry for entry in projection.path}
        path_ids = [entry.id for entry in projection.path]
        assert len(path_ids) == 7
        assert path_ids[1:] == [user_id, snapshot.id, assistant_id, first, second, delta.id]
        assert len(projection.context.messages) == len(projection.context.source_entry_ids)
        # 资料节点在原发生位置贡献已保存的 user 投影，来源映射到真实资料节点 ID。
        for index, source_entry_id in enumerate(projection.context.source_entry_ids):
            entry = by_id[source_entry_id]
            if isinstance(entry, BusinessContextEntry):
                message = projection.context.messages[index]
                assert isinstance(message, UserMessage)
                assert business_user_text(message) == entry.message.content
                assert message.timestamp == entry.message.timestamp
        # 真实消息、工具配对与来源一一对应保持稳定。
        assert [by_id[source].messages[0].role for source in projection.context.source_entry_ids
                if isinstance(by_id[source], SessionMessageEntry)] == [
            "system", "user", "assistant", "toolResult", "toolResult",
        ]
        # 重复读取同一投影一致。
        repeated = await service.queries.get_projection(session_id, delta.id)
        assert message_projection(repeated.context.messages) == message_projection(
            projection.context.messages
        )
        assert repeated.context.source_entry_ids == projection.context.source_entry_ids
        # 内部资料节点不进入 message_nodes 白名单，也不成为真实用户回合或切点。
        visible = set(projection.context.source_entry_ids)
        selected = model_visible_entries(projection.path, visible)
        assert {entry.id for entry in selected} == {
            entry.id for entry in projection.path if isinstance(entry, SessionMessageEntry)
        }
        assert snapshot.id not in {entry.id for entry in selected}
        assert C._is_turn_start(by_id[snapshot.id]) is False
        assert C._is_cut_point(by_id[snapshot.id]) is False
        branch = await service.queries.get_current_branch(session_id)
        assert [entry.id for entry in branch] == [
            projection.path[0].id, user_id, assistant_id, first, second,
        ]

        # 预算：有效投影含资料；资料消息映射到真实节点，usage 之后的资料按增量计入。
        estimate = C.estimate_projected_context_tokens(projection.context, projection.path)
        assert estimate.last_usage_index is not None
        assert estimate.trailing_tokens >= C.estimate_tokens(
            business_context_to_llm(by_id[delta.id].message)
        )
        await database.close()

        # 真实数据库关闭重开后投影一致。
        reopened = await open_database(root / "projection.db")
        restored = SessionService(SqliteSessionRepository(reopened))
        try:
            restored_projection = await restored.queries.get_projection(session_id, delta.id)
            assert message_projection(restored_projection.context.messages) == message_projection(
                projection.context.messages
            )
            assert restored_projection.context.source_entry_ids == projection.context.source_entry_ids
            restored_delta = next(
                entry for entry in restored_projection.path if entry.id == delta.id
            )
            assert restored_delta.message.content == delta.message.content
        finally:
            await reopened.close()
        return {
            "snapshot": snapshot.id, "delta": delta.id,
            "sources": projection.context.source_entry_ids,
        }
    except BaseException:
        await database.close()
        raise


def check_protocol(messages: list[Message], base_prefix: list[Message]) -> None:
    # 两条生产构建器：资料以 user-role 出现在原顺序，固定文本不进入 system 头部。
    openai = openai_completions.to_openai_request(messages, OPENAI, {"api_key": "k"})
    anthropic = anthropic_messages.to_anthropic_request(
        messages, ANTHROPIC, {"api_key": "k", "max_tokens": 1024}
    )
    assert openai["messages"][0] == {"role": "system", "content": SYSTEM_TEXT}
    assert anthropic["system"] == SYSTEM_TEXT
    assert "<business_context>" not in openai["messages"][0]["content"]
    assert "<business_context>" not in anthropic["system"]
    tool_names = [declaration.name for declaration in business_tool_declarations()]
    assert [item["function"]["name"] for item in openai["tools"]] == tool_names
    assert [item["name"] for item in anthropic["tools"]] == tool_names
    business_users = [
        item for item in openai["messages"]
        if item["role"] == "user" and "<business_context>" in str(item["content"])
    ]
    assert business_users
    anthropic_users = [
        item for item in anthropic["messages"]
        if item["role"] == "user" and "<business_context>" in str(item["content"])
    ]
    assert len(anthropic_users) == len(business_users)
    # 工具结果完整连续；资料位于该批全部结果之后。
    openai_roles = [item["role"] for item in openai["messages"]]
    tool_positions = [index for index, role in enumerate(openai_roles) if role == "tool"]
    assert len(tool_positions) == 2
    assert tool_positions[1] == tool_positions[0] + 1
    last_business = max(
        index for index, item in enumerate(openai["messages"])
        if item["role"] == "user" and "<business_context>" in str(item["content"])
    )
    assert tool_positions[-1] < last_business
    # 前缀保持稳定：资料追加不改变基础 system、工具声明与既有历史投影。
    openai_base = openai_completions.to_openai_request(base_prefix, OPENAI, {"api_key": "k"})
    anthropic_base = anthropic_messages.to_anthropic_request(
        base_prefix, ANTHROPIC, {"api_key": "k", "max_tokens": 1024}
    )
    assert openai_base["messages"][0] == openai["messages"][0]
    assert openai_base["tools"] == openai["tools"]
    assert anthropic_base["system"] == anthropic["system"]
    assert anthropic_base["tools"] == anthropic["tools"]
    assert openai["messages"][: len(openai_base["messages"])] == openai_base["messages"]
    # 工具调用 ID 与真实配对保持确定性规范化。
    def tool_blocks(items, role: str, kind: str) -> list[dict]:
        return [
            block
            for item in items
            if item["role"] == role and isinstance(item["content"], list)
            for block in item["content"]
            if isinstance(block, dict) and block["type"] == kind
        ]

    tool_use_ids = [
        block["id"] for block in tool_blocks(anthropic["messages"], "assistant", "tool_use")
    ]
    tool_result_ids = [
        block["tool_use_id"]
        for block in tool_blocks(anthropic["messages"], "user", "tool_result")
    ]
    assert tool_use_ids == ["call-1", "call-2"]
    assert tool_result_ids == ["call-1", "call-2"]


def scripted_stream(turns, captured):
    pending = list(turns)

    def stream_fn(model, context, options):
        captured.append(list(context["messages"]))

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
        run = await start_run(service, session_id, "运行投影")
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
        start_projection = await service.queries.get_projection(session_id, run.request_entry_id)
        loop = asyncio.get_running_loop()
        model = ModelConfig(
            api="openai-completions", base_url="https://example.com/v1",
            model="test-model", provider="openai", api_key="test-secret",
        )
        turns = [
            assistant(
                [("call-1", "get_profile", {}), ("call-2", "get_current_plan", {})],
                "toolUse",
            ),
            assistant_text("完成。"),
        ]
        captured: list = []
        estimates: list[int] = []
        stream_fn = scripted_stream(turns, captured)
        real = http_module.run_agent_loop

        def patched(prompts, context, config, emit, signal=None):
            hooks = config.compaction
            original_estimate = hooks.estimate_request

            async def recording_estimate() -> int:
                value = await original_estimate()
                estimates.append(value)
                return value

            hooks.estimate_request = recording_estimate
            return real(prompts, context, config, emit, signal, stream_fn=stream_fn)

        http_module.run_agent_loop = patched
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    execute, run, session_id, start_projection.context.messages, {},
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
        # 事件计数固定；并行工具的开始与完成顺序不作成对假设。
        assert names.count("custom_message") == 2
        assert names.count("message_start") == 2
        assert names.count("message_end") == 2
        assert names.count("tool_start") == 2
        assert names.count("tool_execution_end") == 2
        assert names.count("tool_result") == 2
        assert names[0] == "custom_message"
        assert names[1:3] == ["message_start", "message_end"]
        first_result = names.index("tool_result")
        second_result = names.index("tool_result", first_result + 1)
        last_end = max(index for index, name in enumerate(names) if name == "tool_execution_end")
        second_custom = names.index("custom_message", names.index("custom_message") + 1)
        # 同批全部工具结果先于该批资料通知；资料通知先于下一次模型消息事件。
        assert last_end < first_result < second_result < second_custom
        assert names[second_custom + 1 :] == ["message_start", "message_end"]
        for name, data in events:
            if name == "custom_message":
                assert set(data) == {"type", "entry_id", "parent_id", "run_id", "created_at"}

        # 每条资料只进入当前请求一次：首个请求含基线，第二个请求含基线与增量。
        assert len(captured) == 2
        first_business = [
            message for message in captured[0]
            if isinstance(message, UserMessage)
            and not isinstance(message.content, str)
            and "<business_context>" in business_user_text(message)
        ]
        second_business = [
            message for message in captured[1]
            if isinstance(message, UserMessage)
            and not isinstance(message.content, str)
            and "<business_context>" in business_user_text(message)
        ]
        assert len(first_business) == 1
        assert len(second_business) == 2
        assert len(captured[0]) == len(start_projection.context.messages) + 1
        # 连续模型调用不重复追加：数据库仅两条资料节点。
        history = await service.queries.get_session_history(session_id)
        custom = [entry for entry in history.entries if entry.type == "custom_message"]
        assert len(custom) == 2
        assert custom[0].parent_id == run.request_entry_id
        tool_entries = [data["entry_id"] for name, data in events if name == "tool_result"]
        assert custom[1].parent_id == tool_entries[-1]

        # 完整请求预算：估算与实际发送同口径，且包含本次调用前将追加的资料。
        assert len(estimates) == 2
        assert estimates[0] == C.estimate_context_tokens(captured[0]).tokens
        assert estimates[1] == C.estimate_context_tokens(captured[1]).tokens
        projection_only = C.estimate_context_tokens(
            start_projection.context.messages
        ).tokens
        assert estimates[0] > projection_only

        # 两条生产构建器：资料按原顺序出现在 user-role 消息中，system 头部不含资料文本。
        check_protocol(captured[1], start_projection.context.messages)
        return {"events": names, "estimates": estimates}
    finally:
        await database.close()


def check_budget_boundary() -> None:
    user_id = uid()
    base = [
        SystemMessage(role="system", content="基础", timestamp=1),
        UserMessage(role="user", content="问题", timestamp=2),
        assistant([], "stop", "回答", usage=Usage(
            input=100, output=50, cache_read=10, cache_write=5, total_tokens=165
        )),
    ]
    estimate = C.estimate_context_tokens(base)
    assert estimate.last_usage_index == 2
    assert estimate.usage_tokens == 165 and estimate.trailing_tokens == 0
    business = business_context_to_llm(
        build_business_context_message(details=snapshot_details(user_id), created_at=4)
    )
    business_tokens = C.estimate_tokens(business)
    extended = C.estimate_context_tokens([*base, business])
    # usage 仅在仍描述当前投影时复用；追加资料按增量计入。
    assert extended.last_usage_index == 2
    assert extended.usage_tokens == 165
    assert extended.trailing_tokens == business_tokens
    assert extended.tokens == 165 + business_tokens
    # 边界附近：仅历史资料在阈值内，加入本次新增资料即越过阈值。
    reserve = 16384
    base_tokens = 50000
    task_window = base_tokens + business_tokens + reserve + C.SAFETY_MARGIN_TOKENS - 1
    assert C.should_compact(base_tokens, task_window, reserve) is False
    assert C.should_compact(base_tokens + business_tokens, task_window, reserve) is True


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        check_conversion()
        check_budget_boundary()
        result = {
            "projection": asyncio.run(scenario_projection(root)),
            "execute": asyncio.run(scenario_execute(root)),
        }
    (EVIDENCE / "business-context-projection.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: 资料消息原位置 user 转换、固定文本与时间保留、display=false 进入模型输入、"
        "服务商 Message 联合边界、输入隔离、当前分支资料重放与重启一致、"
        "资料不进入 message_nodes 与非切点、双适配器前缀与工具声明稳定、"
        "多工具配对与确定性工具 ID、预算含本次新增资料且重复估算不重复写入"
    )


if __name__ == "__main__":
    check()
