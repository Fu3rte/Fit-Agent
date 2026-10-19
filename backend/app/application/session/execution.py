import asyncio
import os
from collections.abc import Callable
from time import time_ns
from typing import Protocol
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop
from app.agent.budget import RequestBudget
from app.agent.cache_observation import CacheObserver
from app.agent.compaction import (
    KEEP_RECENT_TOKENS,
    RESERVE_TOKENS,
    CompactionSettings,
    build_effective_context,
    estimate_context_tokens,
    generate_compaction_summary,
    prepare_compaction,
    project_user_message,
)
from app.agent.config import AgentLoopConfig, CompactionHooks, LoopCompactionResult
from app.agent.governance import TOOL_EXECUTION_SECONDS
from app.agent.message_context import business_context_to_llm
from app.agent.permissions import create_before_tool_call
from app.agent.telemetry import TraceSpan
from app.agent.tool import CredentialDetectedError
from app.agent.tools.business import bind_business_tools
from app.ai.context import get_current_system_message
from app.ai.messages import AssistantMessage, Message, ToolResultMessage
from app.ai.model_capabilities import resolve_model_spec
from app.ai.types import check_cancelled
from app.application.business.profile import BUSINESS_TIMEZONE, business_date
from app.application.business.service import BusinessService
from app.application.session.business_context import (
    BusinessBindings,
    collect_message_nodes,
    collect_pending_proposals,
    next_business_context_event,
)
from app.application.session.credentials import CredentialFilter
from app.application.session.entries import SessionEntries
from app.application.session.queries import SessionQueries
from app.application.session.runtime import RunState
from app.domain.business.models import BusinessContext
from app.domain.session.models import (
    BusinessContextData,
    BusinessContextEntry,
    BusinessContextSnapshot,
    CompactionDetails,
    CompactionEntry,
    RequestContext,
    SessionEntry,
    SessionMessageEntry,
    build_business_context_entry,
    build_business_context_message,
    business_baseline_entry_id,
)


class RunEvents(Protocol):
    async def emit(self, event) -> None: ...

    def tool_update(self, tool_call_id: str, tool_name: str, result) -> None: ...

    def custom_message(self, entry) -> None: ...


PLAN_PREPARE_TOOLS = {"prepare_plan", "prepare_plan_import", "prepare_plan_adjustment"}


def compaction_settings() -> CompactionSettings:
    # 生产预算固定为 reserve 16384、keep 20000；环境变量仅供测试注入缩小或调整触发条件的预算。
    reserve = os.environ.get("FIT_AGENT_COMPACTION_RESERVE_TOKENS")
    keep = os.environ.get("FIT_AGENT_COMPACTION_KEEP_RECENT_TOKENS")
    return CompactionSettings(
        reserve_tokens=int(reserve) if reserve is not None else RESERVE_TOKENS,
        keep_recent_tokens=int(keep) if keep is not None else KEEP_RECENT_TOKENS,
    )


def model_visible_entries(
    path: list[SessionEntry], visible: set[str]
) -> list[SessionMessageEntry]:
    # message_nodes 白名单：只接纳模型可见集合内的真实消息节点。内部资料节点与压缩
    # 结构节点即使参与祖先链读取，其身份也不会成为资料来源。
    return [
        entry
        for entry in path
        if isinstance(entry, SessionMessageEntry) and entry.id in visible
    ]


def execute(
    run,
    session_id: str,
    branch: list[Message],
    tools: dict,
    state: RunState,
    queries: SessionQueries,
    entries: SessionEntries,
    business: BusinessService,
    loop,
    model,
    secrets: tuple[str, ...],
    root: TraceSpan,
    *,
    create_events: Callable[[CredentialFilter, dict[str, str]], RunEvents],
) -> str:
    coordinator = state.coordinator
    guard = CredentialFilter(secrets)
    # 逐模型调用缓存观测：普通调用、摘要子调用与恢复重试均按真实请求记录，身份绑定当前运行。
    observer = (
        state.observation_factory()
        if state.observation_factory is not None
        else CacheObserver(
            session_id=session_id, run_id=run.id, contains=guard.contains
        )
    )
    if state.origin == "edit":
        observer.note_event("edit")
    elif state.origin == "regenerate":
        observer.note_event("branch")
    position = {"id": run.request_entry_id}
    parents: dict[str, str] = {}
    events = create_events(guard, parents)
    # 可信业务身份与日期：request_entry_id 随 Steering 消费更新，日期取该用户节点
    # 的真实 created_at，处理期间固定；每批按 source_entry_id 绑定。
    request_entry = {"id": run.request_entry_id}
    business_day = {"value": ""}
    # 准备调用按业务独立登记；结果节点提交后才绑定展示。
    prepared: dict[str, str] = {}
    workout_prepared: dict[str, str] = {}
    plan_prepared: dict[str, str] = {}

    def call(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def read_business_date(entry_id: str) -> str:
        entry = call(queries.get_entry(session_id, entry_id))
        return business_date(entry.created_at)

    business_day["value"] = read_business_date(request_entry["id"])
    # 运行开始时固定一次模型能力快照与压缩预算，供当前运行与摘要调用共用；请求预算每次运行独立创建。
    spec = resolve_model_spec(model)
    settings = compaction_settings()
    budget = RequestBudget()

    def business_context(source_entry_id: str) -> BusinessContext:
        return BusinessContext(
            timezone=BUSINESS_TIMEZONE,
            business_date=business_day["value"],
            session_id=session_id,
            run_id=run.id,
            request_entry_id=request_entry["id"],
            source_entry_id=source_entry_id,
        )

    def execution_registry(source_entry_id: str) -> dict:
        return {
            **tools,
            **bind_business_tools(
                business, business_context(source_entry_id), call, prepared,
                workout_prepared, plan_prepared
            ),
        }

    announce_business_context = events.custom_message

    async def read_request_state() -> tuple[list[Message], BusinessContextData | None]:
        # 主循环读取：当前有效模型投影与本次模型调用前将追加的资料；只读，不提交、不通知。
        projection = await queries.get_projection(session_id, position["id"])
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
        details = await next_business_context_event(
            path=projection.path,
            request=RequestContext(
                timezone=BUSINESS_TIMEZONE,
                business_date=business_day["value"],
                request_entry_id=request_entry["id"],
            ),
            nodes=nodes,
            proposals=proposals,
            removal_reason=business.proposals.removed_proposal_reason,
        )
        return projection.context.messages, details

    async def append_business_context() -> BusinessContextEntry | None:
        # 下一次模型请求前比较真实资料并提交；提交失败或凭据命中时抛错，调用方不得发送身份通知。
        _, details = await read_request_state()
        if details is None:
            return None
        node = build_business_context_entry(
            session_id=session_id,
            id=str(uuid4()),
            parent_id=position["id"],
            run_id=run.id,
            details=details,
            created_at=time_ns() // 1_000_000,
        )
        outcome = await entries.append_business_context(node, credentials=secrets)
        if outcome.credential_detected:
            raise CredentialDetectedError()
        return outcome.entry

    async def transform_context(messages: list[Message], signal) -> list[Message]:
        # 每次模型调用前从真实持久化分支重放完整有效资料序列：必要资料提交成功才通知身份并同步
        # 真实执行位置，使后续助手节点的 parent_id 指向最新资料节点；同批工具结果与 Steering 消费
        # 通知均先于本批资料通知。不依赖会遗漏隐藏节点的内存消息列表，每条资料只进入本次请求一次。
        committed = call(append_business_context())
        if committed is not None:
            announce_business_context(committed)
            position["id"] = committed.id
            observer.note_event("business_context")
        projection = call(queries.get_projection(session_id, position["id"]))
        return list(projection.context.messages)

    async def save_message(node_id: str, message: Message) -> None:
        parent_id = position["id"]

        async def save() -> object:
            return await entries.append_entry(
                SessionMessageEntry(
                    session_id=session_id,
                    id=node_id,
                    parent_id=parent_id,
                    run_id=run.id,
                    type="message",
                    messages=[message],
                    created_at=time_ns() // 1_000_000,
                    # 明确上下文溢出的失败尝试：持久化模型投影省略标记。
                    model_omitted=(
                        isinstance(message, AssistantMessage)
                        and message.context_overflow
                    ),
                ),
                credentials=secrets,
            )

        outcome = asyncio.run_coroutine_threadsafe(save(), loop).result()
        if outcome.credential_detected:
            raise CredentialDetectedError()
        parents[node_id] = parent_id
        position["id"] = node_id
        if (
            isinstance(message, ToolResultMessage)
            and message.tool_name in {*PLAN_PREPARE_TOOLS, "prepare_profile_update", "prepare_workout"}
            and not message.is_error
        ):
            # 成功结果必须对应本批登记；关联缺失直接终止运行。
            if message.tool_name == "prepare_profile_update":
                call(business.profile.bind_display_entry(prepared.pop(message.tool_call_id), node_id))
            elif message.tool_name == "prepare_workout":
                call(business.workouts.bind_workout_display_entry(
                    workout_prepared.pop(message.tool_call_id), node_id
                ))
            else:
                call(business.plans.bind_plan_display_entry(
                    plan_prepared.pop(message.tool_call_id), node_id
                ))

    async def get_steering_messages() -> list[Message]:
        return call(coordinator.take(run.id))

    async def get_steering_messages_or_close() -> list[Message]:
        return call(coordinator.take_or_close(run.id))

    def project_steering(messages: list[Message]) -> list[Message]:
        # 统一附件投影：Steering 输入在进入模型请求前表达附件读取引用。
        groups = call(coordinator.attachments_for(run.id, messages))
        return [
            project_user_message(message, group)
            for message, group in zip(messages, groups)
        ]

    async def on_steering_consumed(messages: list[Message]) -> None:
        last_id = call(coordinator.consume(run.id, messages))
        if last_id is not None:
            position["id"] = last_id
            # Steering 消费后：后续批次的 request_entry_id 与业务日期使用新用户节点。
            request_entry["id"] = last_id
            business_day["value"] = read_business_date(last_id)

    emit = events.emit
    on_tool_update = events.tool_update

    def request_messages() -> list[Message]:
        # 即将发送的完整请求：当前有效模型投影加本次模型调用前将追加的资料，与实际发送同口径。
        messages, details = call(read_request_state())
        if details is None:
            return messages
        return [
            *messages,
            business_context_to_llm(
                build_business_context_message(
                    details=details, created_at=time_ns() // 1_000_000
                )
            ),
        ]

    async def estimate_request() -> int:
        # 完整计数入口：来源与投影一致性由投影读取保证，本次将追加的资料与实际发送同口径计入。
        return estimate_context_tokens(request_messages()).tokens

    async def build_compaction_baseline(
        path: list[SessionEntry], entry: CompactionEntry
    ) -> BusinessContextEntry:
        # 压缩新基线：按拟提交检查点形成的压缩后有效投影重新采集可见真实消息来源，
        # 待确认提案按完整当前分支核验（被摘要的真实来源仍受控保留）；强制完整 Snapshot。
        prospective = build_effective_context([*path, entry])
        visible = set(prospective.source_entry_ids)
        nodes = collect_message_nodes(
            model_visible_entries([*path, entry], visible),
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
            {item.id for item in path},
        )
        snapshot = BusinessContextSnapshot(
            mode="snapshot",
            request=RequestContext(
                timezone=BUSINESS_TIMEZONE,
                business_date=business_day["value"],
                request_entry_id=request_entry["id"],
            ),
            message_nodes=list(nodes.values()),
            pending_proposals=list(proposals.values()),
        )
        return build_business_context_entry(
            session_id=session_id,
            id=business_baseline_entry_id(entry.id),
            parent_id=entry.id,
            run_id=run.id,
            details=snapshot,
            created_at=time_ns() // 1_000_000,
        )

    async def compact(compaction_id: str, signal, span) -> LoopCompactionResult:
        # 事务外生成真实摘要与用量并重建完整新基线，再经同一事务入口原子提交；cancelled 在提交前生效。
        check_cancelled(signal)
        projection = call(queries.get_projection(session_id, position["id"]))
        preparation = prepare_compaction(
            projection.path, spec.context_window, spec.max_tokens, settings
        )
        tokens_before = estimate_context_tokens(request_messages()).tokens
        # 摘要 model 子 Span 挂在当前 Compaction 下，与检查点提交同属一次压缩操作。
        result = await generate_compaction_summary(
            preparation, spec, api_key=secrets[0], settings=settings, signal=signal,
            deadline=state.control.deadline,
            run_control=state.control,
            budget=budget, observation=observer, compaction_id=compaction_id, parent=span,
        )
        check_cancelled(signal)
        system_state = get_current_system_message(projection.context.messages)
        if system_state is None:
            raise RuntimeError("压缩检查点缺少稳定系统状态")
        entry = CompactionEntry(
            session_id=session_id,
            id=compaction_id,
            parent_id=position["id"],
            run_id=run.id,
            type="compaction",
            summary=result.summary,
            first_kept_entry_id=preparation.first_kept_entry_id,
            tokens_before=tokens_before,
            usage=result.usage,
            system_message=system_state,
            details=(
                CompactionDetails(attachments=preparation.attachments)
                if preparation.attachments
                else None
            ),
            created_at=time_ns() // 1_000_000,
        )
        baseline = call(build_compaction_baseline(projection.path, entry))
        outcome = call(entries.commit_compaction(
            entry, baseline, prepared_position=entry.parent_id, credentials=secrets
        ))
        if outcome.credential_detected:
            raise CredentialDetectedError()
        parents[outcome.entry.id] = outcome.entry.parent_id
        parents[outcome.baseline.id] = outcome.entry.id
        # 压缩提交后标注已知事件：供检查点之后的首次真实调用记录重置原因。
        observer.note_event("compaction")
        # 执行位置与刷新投影使用新基线节点；后续助手节点 parent_id、工具与 Steering 追加位置随之一致。
        position["id"] = outcome.baseline.id
        refreshed = call(queries.get_projection(session_id, outcome.baseline.id))
        return LoopCompactionResult(
            entry_id=outcome.entry.id,
            parent_id=outcome.entry.parent_id,
            messages=refreshed.context.messages,
            baseline=outcome.baseline,
        )

    loop_config = AgentLoopConfig(
        model=model,
        max_turns=64,
        model_spec=spec,
        request_budget=budget,
        deadline=state.control.deadline,
        run_control=state.control,
        tool_tracker=state.tool_tracker,
        tool_timeout_seconds=TOOL_EXECUTION_SECONDS,
        compaction=CompactionHooks(
            estimate_request=estimate_request,
            compact=compact,
            context_window=spec.context_window,
            reserve_tokens=settings.reserve_tokens,
        ),
        transform_context=transform_context,
        get_steering_messages=get_steering_messages,
        get_steering_messages_or_close=get_steering_messages_or_close,
        project_steering=project_steering,
        on_steering_consumed=on_steering_consumed,
        save_message=save_message,
        bind_tools=execution_registry,
        before_tool_call=create_before_tool_call(),
        on_tool_update=on_tool_update,
        contains_credentials=guard.contains,
        observation=observer,
        trace_span=root,
    )
    # 稳定声明注册表：业务工具实例仅用于声明校验，实际执行按批重新绑定。
    context = {
        "messages": branch[:-1],
        "tools": execution_registry(run.request_entry_id),
    }
    messages = asyncio.run(
        run_agent_loop([branch[-1]], context, loop_config, emit, state.cancel)
    )
    check_cancelled(state.cancel)
    final_message = next(
        message for message in reversed(messages)
        if isinstance(message, AssistantMessage) and message.stop_reason == "stop"
    )
    return final_message.stop_reason
