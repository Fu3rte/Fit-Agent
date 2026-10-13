from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from app.ai.messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from app.domain.session.models import (
    AssistantNodeReference,
    BusinessContextData,
    BusinessContextDelta,
    BusinessContextEntry,
    BusinessContextSnapshot,
    MessageNodeReference,
    PendingProposalReference,
    ProposalKey,
    RemovedPendingProposal,
    RequestContext,
    SessionEntry,
    SessionMessageEntry,
    ToolResultNodeReference,
    UserNodeReference,
)

MessageNodeState = dict[str, MessageNodeReference]
ProposalState = dict[tuple[str, str], PendingProposalReference]
TrackedState = tuple[RequestContext, MessageNodeState, ProposalState]


@dataclass(frozen=True)
class BusinessBindings:
    # 后端已保存的确认与展示关联：用户节点与工具结果节点据此确定业务提案身份。
    profile_confirmations: Mapping[str, str]
    workout_confirmations: Mapping[str, str]
    plan_confirmations: Mapping[str, str]
    profile_displays: Mapping[str, str]
    workout_displays: Mapping[str, str]
    plan_displays: Mapping[str, str]


def _proposal_key(
    *candidates: tuple[str, str | None],
) -> ProposalKey | None:
    # 一个节点最多绑定一类业务提案：多类同时命中属后端关联冲突，就地报错。
    found = [
        (business_kind, proposal_id)
        for business_kind, proposal_id in candidates
        if proposal_id is not None
    ]
    if len(found) > 1:
        raise RuntimeError("节点具有多类业务绑定")
    if not found:
        return None
    business_kind, proposal_id = found[0]
    return ProposalKey(business_kind=business_kind, proposal_id=proposal_id)


def collect_message_nodes(
    entries: Sequence[SessionMessageEntry], bindings: BusinessBindings
) -> MessageNodeState:
    # 三类来源按真实角色映射：确认与展示关联取后端持久化事实，工具调用保持真实配对。
    nodes: MessageNodeState = {}
    for entry in entries:
        message = entry.messages[0]
        if isinstance(message, UserMessage):
            nodes[entry.id] = UserNodeReference(
                entry_id=entry.id,
                role="user",
                confirmation=entry.confirmation,
                proposal=_proposal_key(
                    ("profile", bindings.profile_confirmations.get(entry.id)),
                    ("workout", bindings.workout_confirmations.get(entry.id)),
                    ("plan", bindings.plan_confirmations.get(entry.id)),
                ),
            )
        elif isinstance(message, AssistantMessage):
            nodes[entry.id] = AssistantNodeReference(
                entry_id=entry.id,
                role="assistant",
                tool_call_ids=[
                    block.id
                    for block in message.content
                    if isinstance(block, ToolCall)
                ],
            )
        elif isinstance(message, ToolResultMessage):
            nodes[entry.id] = ToolResultNodeReference(
                entry_id=entry.id,
                role="toolResult",
                tool_name=message.tool_name,
                tool_call_id=message.tool_call_id,
                proposal=_proposal_key(
                    ("profile", bindings.profile_displays.get(entry.id)),
                    ("workout", bindings.workout_displays.get(entry.id)),
                    ("plan", bindings.plan_displays.get(entry.id)),
                ),
            )
    return nodes


def collect_pending_proposals(
    references: Sequence[object], path_ids: set[str]
) -> ProposalState:
    # 待确认提案使用完整当前分支核验：请求、来源与已绑定展示节点都必须仍在当前路径上。
    # 后端读取结果映射为会话资料强类型字段，跨模型转换不可省略。
    proposals: ProposalState = {}
    for reference in references:
        if (
            reference.request_entry_id not in path_ids
            or reference.source_entry_id not in path_ids
        ):
            continue
        if (
            reference.display_entry_id is not None
            and reference.display_entry_id not in path_ids
        ):
            continue
        proposals[(reference.business_kind, reference.proposal_id)] = (
            PendingProposalReference(
                business_kind=reference.business_kind,
                proposal_id=reference.proposal_id,
                status=reference.status,
                request_entry_id=reference.request_entry_id,
                source_entry_id=reference.source_entry_id,
                display_entry_id=reference.display_entry_id,
                confirmation_entry_id=reference.confirmation_entry_id,
            )
        )
    return proposals


def tracked_state(path: Sequence[SessionEntry]) -> TrackedState | None:
    # 当前分支资料状态：从最近完整基线开始，按祖先链顺序应用后续增量。
    # 资料节点自身不参与状态，避免递归资料增长。
    state: TrackedState | None = None
    for entry in path:
        if not isinstance(entry, BusinessContextEntry):
            continue
        details = entry.message.details
        if isinstance(details, BusinessContextSnapshot):
            state = (
                details.request,
                {node.entry_id: node for node in details.message_nodes},
                {
                    (proposal.business_kind, proposal.proposal_id): proposal
                    for proposal in details.pending_proposals
                },
            )
            continue
        if state is None:
            raise RuntimeError("分支首个资料事件必须是完整基线")
        request, nodes, proposals = state
        next_nodes = dict(nodes)
        next_proposals = dict(proposals)
        for node in details.upsert_message_nodes:
            next_nodes[node.entry_id] = node
        for node_id in details.remove_message_node_ids:
            next_nodes.pop(node_id, None)
        for proposal in details.upsert_pending_proposals:
            next_proposals[(proposal.business_kind, proposal.proposal_id)] = proposal
        for removed in details.remove_pending_proposals:
            next_proposals.pop((removed.business_kind, removed.proposal_id), None)
        state = (
            details.request if details.request is not None else request,
            next_nodes,
            next_proposals,
        )
    return state


async def next_business_context_event(
    *,
    path: Sequence[SessionEntry],
    request: RequestContext,
    nodes: MessageNodeState,
    proposals: ProposalState,
    removal_reason: Callable[[str, str], Awaitable[str]],
) -> BusinessContextData | None:
    # 首次资料事件保存完整基线；已有基线时按真实变化生成增量；无变化返回 None，不构造空增量。
    state = tracked_state(path)
    if state is None:
        return BusinessContextSnapshot(
            mode="snapshot",
            request=request,
            message_nodes=list(nodes.values()),
            pending_proposals=list(proposals.values()),
        )
    previous_request, previous_nodes, previous_proposals = state
    upsert_nodes = [
        node for entry_id, node in nodes.items() if previous_nodes.get(entry_id) != node
    ]
    remove_nodes = [
        entry_id for entry_id in previous_nodes if entry_id not in nodes
    ]
    upsert_proposals = [
        proposal
        for key, proposal in proposals.items()
        if previous_proposals.get(key) != proposal
    ]
    removed_proposals: list[RemovedPendingProposal] = []
    for key, _previous in previous_proposals.items():
        if key in proposals:
            continue
        # 移出原因必须有真实状态、删除或有效范围证据；无法确定时由判定处停止报告。
        reason = await removal_reason(key[0], key[1])
        removed_proposals.append(
            RemovedPendingProposal(
                business_kind=key[0], proposal_id=key[1], reason=reason
            )
        )
    request_field = request if request != previous_request else None
    if (
        request_field is None
        and not upsert_nodes
        and not remove_nodes
        and not upsert_proposals
        and not removed_proposals
    ):
        return None
    return BusinessContextDelta(
        mode="delta",
        request=request_field,
        upsert_message_nodes=upsert_nodes,
        remove_message_node_ids=remove_nodes,
        upsert_pending_proposals=upsert_proposals,
        remove_pending_proposals=removed_proposals,
    )
