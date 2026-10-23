import hashlib
import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.cache_observation import CacheObserver, sanitize_endpoint
from app.ai.messages import (
    AssistantMessage,
    Message,
    TextContent,
    ToolCall,
    ToolResultMessage,
    text_projection,
)
from app.ai.types import CallObservation, ModelSpec
from app.application.session.queries import SessionQueries
from app.domain.business.models import (
    PendingProposalCancellation,
    PlanPendingProposal,
    PlanSaveRecord,
    ProfilePendingProposal,
    ProfileSaveRecord,
    SaveConfirmationOperation,
    WorkoutListArguments,
    WorkoutPendingProposal,
    WorkoutRecord,
    WorkoutSaveRecord,
)
from app.domain.business.repository import BusinessRepository
from app.domain.session.models import (
    BusinessContextEntry,
    CompactionEntry,
    SessionMessageEntry,
)
from evaluation.contracts import (
    BusinessProjection,
    ContextItem,
    ContextSource,
    MessageNode,
    ModelVisibleSearch,
    NonEmptyText,
    ProposalBinding,
    SearchResponseEvidence,
    ToolNode,
    VisibleExercise,
)
from evaluation.deterministic_assertions import EventTarget

BUSINESS_CONTEXT_TAG = "<business_context>"


class ScenarioEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ContextMappingError(ValueError):
    """模型可见正文与真实持久化内容无法一致表达。"""


class ContextGap(ScenarioEvidenceModel):
    reason: NonEmptyText
    role: str | None = None
    message_entry_id: NonEmptyText | None = None
    tool_call_id: NonEmptyText | None = None
    content_sha256: NonEmptyText | None = None
    ambiguous: bool = False


class ContextMapping(ScenarioEvidenceModel):
    items: list[ContextItem]
    gaps: list[ContextGap]
    tool_identity_map: dict[str, str]

    @property
    def ambiguous(self) -> bool:
        return any(gap.ambiguous for gap in self.gaps)


class CancelEvidence(ScenarioEvidenceModel):
    tool_call_id: NonEmptyText
    business_kind: str
    proposal_id: NonEmptyText
    result: dict[str, Any]
    session_id: NonEmptyText
    run_id: NonEmptyText
    request_entry_id: NonEmptyText
    source_entry_id: NonEmptyText
    result_entry_id: NonEmptyText


class EvaluationCallRecord(ScenarioEvidenceModel):
    sequence: int = Field(ge=0)
    call_kind: NonEmptyText
    message_id: NonEmptyText | None = None
    compaction_id: NonEmptyText | None = None
    api: NonEmptyText
    provider: NonEmptyText
    model: NonEmptyText
    request_sha256: NonEmptyText
    request_path: NonEmptyText
    status: NonEmptyText | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    usage: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _existing_sequence(path: Path) -> int:
    if not path.exists():
        return 0
    sequences = [
        int(json.loads(line)["sequence"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return 0 if not sequences else max(sequences) + 1


class EvaluationCallRecorder:
    """真实模型调用全文证据：显式装配、仅写隔离目录，钩子不改变请求与执行结果。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._sequence = _existing_sequence(directory / "calls.jsonl")
        self._records: list[EvaluationCallRecord] = []
        self._requests: dict[int, dict] = {}
        self._index_path = directory / "calls.jsonl"

    @property
    def records(self) -> list[EvaluationCallRecord]:
        with self._lock:
            return list(self._records)

    @property
    def requests(self) -> dict[int, dict]:
        with self._lock:
            return dict(self._requests)

    def record_request(
        self,
        request: dict,
        model: ModelSpec,
        call_kind: str,
        message_id: str | None,
        compaction_id: str | None,
        *,
        contains: Callable[[object], bool] | None = None,
    ) -> int:
        payload = canonical_json(request)
        if contains is not None and contains(request):
            raise ValueError("模型请求包含受保护凭据，拒绝写入评测全文证据")
        digest = sha256_text(payload)
        with self._lock:
            sequence = self._sequence
            self._sequence += 1
            relative = PurePosixPath("requests") / f"{sequence:06d}-{digest[:12]}.json"
            target = self._directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(target, payload)
            record = EvaluationCallRecord(
                sequence=sequence,
                call_kind=call_kind,
                message_id=message_id,
                compaction_id=compaction_id,
                api=model.api,
                provider=model.provider,
                model=model.id,
                request_sha256=digest,
                request_path=str(relative),
            )
            self._records.append(record)
            self._requests[sequence] = request
            with self._index_path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
            return sequence

    def record_outcome(self, sequence: int, status: str, usage,
                       failure: dict | None = None) -> None:
        with self._lock:
            record = next(item for item in self._records if item.sequence == sequence)
            updated = record.model_copy(update={
                "status": status,
                "input_tokens": None if usage is None else int(usage.input),
                "output_tokens": None if usage is None else int(usage.output),
                "usage": None if usage is None else usage.model_dump(mode="json"),
                "failure": failure,
            })
            position = self._records.index(record)
            self._records[position] = updated
            with self._index_path.open("a", encoding="utf-8") as handle:
                handle.write(updated.model_dump_json() + "\n")


class _EvaluationCallHandle(CallObservation):
    def __init__(
        self,
        inner: CallObservation,
        recorder: EvaluationCallRecorder,
        call_kind: str,
        message_id: str | None,
        compaction_id: str | None,
        contains: Callable[[object], bool] | None,
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._call_kind = call_kind
        self._message_id = message_id
        self._compaction_id = compaction_id
        self._contains = contains
        self._sequence: int | None = None

    @property
    def sequence(self) -> int | None:
        return self._sequence

    @property
    def finished(self) -> bool:
        return self._inner.finished

    def record_first_text(self) -> None:
        self._inner.record_first_text()

    def observe(self, request: dict, model: ModelSpec) -> None:
        self._inner.observe(request, model)
        self._sequence = self._recorder.record_request(
            request,
            model,
            self._call_kind,
            self._message_id,
            self._compaction_id,
            contains=self._contains,
        )

    def finish(self, usage, status: str) -> None:
        if self._sequence is not None:
            self._recorder.record_outcome(self._sequence, status, usage)
        self._inner.finish(usage, status)

    def fail(self, aborted: bool, error_info: dict | None = None) -> None:
        if self._sequence is not None:
            self._recorder.record_outcome(
                self._sequence, "aborted" if aborted else "failed", None, error_info
            )
        self._inner.fail(aborted, error_info)


class EvaluationObserver(CacheObserver):
    """生产 CacheObserver 的评测扩展：保留指纹链，同时显式记录真实请求全文。"""

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        directory: Path,
        contains: Callable[[object], bool] | None,
        recorder: EvaluationCallRecorder,
    ) -> None:
        super().__init__(
            session_id=session_id,
            run_id=run_id,
            directory=directory,
            contains=contains,
        )
        self._recorder = recorder

    def start_call(
        self,
        call_kind: str,
        *,
        message_id: str | None = None,
        compaction_id: str | None = None,
        span=None,
    ) -> CallObservation:
        inner = super().start_call(
            call_kind,
            message_id=message_id,
            compaction_id=compaction_id,
            span=span,
        )
        return _EvaluationCallHandle(
            inner,
            self._recorder,
            call_kind,
            message_id,
            compaction_id,
            self._contains,
        )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


async def read_business_projection(
    repository: BusinessRepository,
    session_ids: Sequence[str],
) -> BusinessProjection:
    """读取完整业务投影：已保存内容、全部提案、确认账本与快照状态事实。"""
    async with repository.transaction():
        profile = await repository.get_profile()
        plans = await repository.list_plans()
        workouts = await _all_workouts(repository)
        save_records: list[ProfileSaveRecord | PlanSaveRecord | WorkoutSaveRecord] = []
        proposals: list[
            ProfilePendingProposal | PlanPendingProposal | WorkoutPendingProposal
        ] = []
        operations: list[SaveConfirmationOperation] = []
        for session_id in session_ids:
            save_records.extend(await repository.list_save_records(session_id))
            save_records.extend(await repository.list_plan_save_records(session_id))
            save_records.extend(await repository.list_workout_save_records(session_id))
            for snapshot in await repository.list_snapshots(session_id):
                proposal = _profile_pending(snapshot)
                if proposal is not None:
                    proposals.append(proposal)
            for snapshot in await repository.list_plan_snapshots(session_id):
                proposal = _plan_pending(snapshot)
                if proposal is not None:
                    proposals.append(proposal)
            for snapshot in await repository.list_workout_snapshots(session_id):
                proposal = _workout_pending(snapshot)
                if proposal is not None:
                    proposals.append(proposal)
            operations.extend(await repository.list_confirmation_operations(session_id))
    return BusinessProjection(
        profile=profile,
        plans=plans,
        workouts=workouts,
        save_records=save_records,
        proposals=proposals,
        confirmation_operations=operations,
        cancellations=[],
    )


async def read_business_projection_with_cancellations(
    repository: BusinessRepository,
    queries: SessionQueries,
    session_ids: Sequence[str],
) -> BusinessProjection:
    """取消记录只来自成功的 cancel_pending_proposal 工具结果。"""
    projection = await read_business_projection(repository, session_ids)
    evidence = await read_cancel_evidence(queries, session_ids)
    return projection.model_copy(update={
        "cancellations": [
            PendingProposalCancellation(
                business_kind=item.business_kind,
                proposal_id=item.proposal_id,
            )
            for item in evidence
        ]
    })


async def _all_workouts(repository: BusinessRepository) -> list[WorkoutRecord]:
    page = 1
    collected: list[WorkoutRecord] = []
    while True:
        result = await repository.list_workouts(
            WorkoutListArguments(page=page, page_size=100)
        )
        collected.extend(result.items)
        if len(collected) >= result.total:
            break
        if not result.items:
            raise RuntimeError("训练记录分页未覆盖全部记录")
        page += 1
    if len(collected) != result.total:
        raise RuntimeError("训练记录分页数量与 total 不一致")
    return collected


def _profile_pending(snapshot):
    if snapshot.display_entry_id is None:
        return None
    return ProfilePendingProposal(
        business_kind="profile",
        proposal_id=snapshot.proposal_id,
        status=snapshot.status,
        payload=snapshot.payload,
        request_entry_id=snapshot.request_entry_id,
        source_entry_id=snapshot.source_entry_id,
        display_entry_id=snapshot.display_entry_id,
        confirmation_entry_id=snapshot.confirmation_entry_id,
        profile_id=snapshot.profile_id,
        base_profile_version=snapshot.base_profile_version,
    )


def _plan_pending(snapshot):
    if snapshot.display_entry_id is None:
        return None
    return PlanPendingProposal(
        business_kind="plan",
        proposal_id=snapshot.proposal_id,
        status=snapshot.status,
        payload=snapshot.payload,
        request_entry_id=snapshot.request_entry_id,
        source_entry_id=snapshot.source_entry_id,
        display_entry_id=snapshot.display_entry_id,
        confirmation_entry_id=snapshot.confirmation_entry_id,
        preparation_kind=snapshot.preparation_kind,
        base_profile_version=snapshot.base_profile_version,
        base_plan_id=snapshot.base_plan_id,
    )


def _workout_pending(snapshot):
    if snapshot.display_entry_id is None:
        return None
    return WorkoutPendingProposal(
        business_kind="workout",
        proposal_id=snapshot.proposal_id,
        status=snapshot.status,
        payload=snapshot.payload,
        request_entry_id=snapshot.request_entry_id,
        source_entry_id=snapshot.source_entry_id,
        display_entry_id=snapshot.display_entry_id,
        confirmation_entry_id=snapshot.confirmation_entry_id,
        performed_on=snapshot.performed_on,
        base_workout_id=snapshot.base_workout_id,
        base_workout_version=snapshot.base_workout_version,
    )


def proposal_payload(proposal, business_kind: str):
    if proposal.business_kind != business_kind:
        raise ValueError("提案业务类型与目标不一致")
    return proposal.payload


def build_proposal_bindings(
    projection: BusinessProjection,
    entry_to_event: dict[str, str],
    confirmation_operations: dict[str, str] | None = None,
) -> dict[str, ProposalBinding]:
    """由真实快照登记 proposal_ref 到实际提案、展示与确认身份的唯一映射。"""
    result: dict[str, ProposalBinding] = {}
    operations = confirmation_operations or {}
    for proposal in projection.proposals:
        event_id = entry_to_event.get(proposal.request_entry_id)
        if event_id is None:
            continue
        reference = f"{proposal.business_kind}-proposal-{event_id}"
        if reference in result:
            raise ValueError(f"proposal_ref 存在多个真实提案: {reference}")
        result[reference] = ProposalBinding(
            proposal_ref=reference,
            business_kind=proposal.business_kind,
            proposal_id=proposal.proposal_id,
            display_entry_id=proposal.display_entry_id,
            confirmation_entry_id=proposal.confirmation_entry_id,
            confirmation_operation_id=operations.get(reference),
        )
    return result


def display_content_for(
    entry,
    business_kind: str,
) -> tuple[dict[str, Any], str | None, str | None]:
    if not isinstance(entry, SessionMessageEntry):
        raise ValueError("展示节点不是消息节点")
    message = entry.messages[0]
    if not isinstance(message, ToolResultMessage):
        raise ValueError("展示节点不是工具结果")
    text = text_projection(message.content)
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise ValueError("展示节点正文不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("展示节点正文必须为 JSON 对象")
    return payload, message.tool_call_id, message.tool_name


def content_text(content) -> str:
    # 协议消息允许 content=null（仅工具调用的助手消息），按空正文处理。
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        elif isinstance(block, TextContent):
            parts.append(block.text)
    return "".join(parts)


def _tool_call_names(messages: Sequence[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if call.get("id") and function.get("name"):
                    names[call["id"]] = function["name"]
            for block in extract_anthropic_blocks(message):
                if block.get("type") == "tool_use" and block.get("id"):
                    names[block["id"]] = block.get("name", "")
    return names


@dataclass(frozen=True)
class RequestContextEntry:
    kind: str
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass(frozen=True)
class RequestProjection:
    events: list[RequestContextEntry | ContextGap]


def request_context_projection(request: dict) -> RequestProjection:
    """按协议消息顺序投影模型输入；正文、角色与工具身份全部来自真实请求。"""
    raw_messages = list(request.get("messages") or [])
    names = _tool_call_names(raw_messages)
    events: list[RequestContextEntry | ContextGap] = []
    system = request.get("system")
    if isinstance(system, str) and system.strip():
        events.append(RequestContextEntry(kind="system", content=system))
    elif raw_messages and raw_messages[0].get("role") == "system":
        text = content_text(raw_messages[0].get("content", ""))
        if text.strip():
            events.append(RequestContextEntry(kind="system", content=text))
    for message in raw_messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "tool":
            tool_call_id = message.get("tool_call_id") or ""
            if not tool_call_id:
                continue
            events.append(RequestContextEntry(
                kind="tool",
                content=content_text(message.get("content", "")),
                tool_call_id=tool_call_id,
                tool_name=names.get(tool_call_id),
            ))
            continue
        if role == "user":
            blocks = extract_anthropic_blocks(message)
            tool_blocks = [
                block for block in blocks if block.get("type") == "tool_result"
            ]
            if tool_blocks:
                for block in tool_blocks:
                    tool_call_id = block.get("tool_use_id") or ""
                    if not tool_call_id:
                        continue
                    events.append(RequestContextEntry(
                        kind="tool",
                        content=content_text(block.get("content", "")),
                        tool_call_id=tool_call_id,
                        tool_name=names.get(tool_call_id),
                    ))
                for block in blocks:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        events.append(RequestContextEntry(
                            kind="user", content=block["text"]
                        ))
                continue
            text = content_text(message.get("content", ""))
            if text.strip():
                events.append(RequestContextEntry(kind="user", content=text))
            continue
        if role == "assistant":
            text = content_text(message.get("content", ""))
            blocks = extract_anthropic_blocks(message)
            tool_calls = message.get("tool_calls") or [
                block for block in blocks if block.get("type") == "tool_use"
            ]
            if not text.strip():
                if tool_calls:
                    events.append(RequestContextEntry(
                        kind="assistant_tool_only", content=text
                    ))
                continue
            events.append(RequestContextEntry(kind="assistant", content=text))
            continue
        events.append(ContextGap(
            reason=f"未知协议角色: {role}",
            role=str(role),
            content_sha256=sha256_text(content_text(message.get("content", ""))),
            ambiguous=True,
        ))
    return RequestProjection(events=events)


def extract_anthropic_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, list):
        return content
    return []


def normalize_tool_id(identifier: str, api: str) -> str:
    """与协议适配器一致的模型可见工具 ID：非法标识按 SHA-256 规范化。"""
    if api == "anthropic-messages":
        pattern, prefix = r"[A-Za-z0-9_-]{1,64}", "toolu_"
    else:
        pattern, prefix = r"[A-Za-z0-9_-]{1,40}", "call_"
    if re.fullmatch(pattern, identifier):
        return identifier
    return prefix + hashlib.sha256(identifier.encode()).hexdigest()[:32]


def _node_kind(entry: object) -> str:
    if isinstance(entry, BusinessContextEntry):
        return "business_context"
    if isinstance(entry, CompactionEntry):
        return "compaction"
    return "message"


@dataclass(frozen=True)
class RequestNodes:
    # 一次真实请求的候选池：消息正文、身份与工具结果全部来自同一投影边界。
    nodes: list[MessageNode]
    tool_nodes: list[ToolNode]


async def read_request_nodes(
    queries: SessionQueries, session_id: str, leaf_id: str
) -> RequestNodes:
    # 节点逐条取该 leaf 的 canonical 投影：附件引用行与压缩摘要前后缀由生产投影给出，
    # 身份与顺序由 source_entry_ids 给出，候选域与冲突域都限定在本次真实请求之内。
    projection = await queries.get_projection(session_id, leaf_id)
    entries = {entry.id: entry for entry in projection.path}
    nodes: list[MessageNode] = []
    tool_nodes: list[ToolNode] = []
    for message, entry_id in zip(
        projection.context.messages, projection.context.source_entry_ids
    ):
        entry = entries[entry_id]
        if isinstance(message, ToolResultMessage):
            tool_nodes.append(ToolNode(
                tool_call_id=message.tool_call_id,
                tool_name=message.tool_name,
                content=text_projection(message.content),
                entry_id=entry.id,
                session_id=session_id,
                run_id=entry.run_id,
                is_error=message.is_error,
            ))
            continue
        nodes.append(MessageNode(
            entry_id=entry.id,
            session_id=session_id,
            role=message.role,
            content=message_visible_text(message),
            kind=_node_kind(entry),
            has_attachments=isinstance(entry, SessionMessageEntry)
            and bool(entry.attachments),
            run_id=entry.run_id,
        ))
    return RequestNodes(nodes=nodes, tool_nodes=tool_nodes)


def tool_nodes_by_visible(
    tool_nodes: Sequence[ToolNode], api: str
) -> dict[str, ToolNode]:
    """持久化工具节点按模型可见 ID 建索引；同一 ID 的多份结果属身份冲突。"""
    nodes: dict[str, ToolNode] = {}
    owners: dict[str, str] = {}
    for node in tool_nodes:
        visible = normalize_tool_id(node.tool_call_id, api)
        owner = owners.get(visible)
        if owner is not None and owner != node.entry_id:
            raise ContextMappingError(
                f"同一工具调用 ID 存在多份持久化结果: {visible}"
            )
        nodes[visible] = node
        owners[visible] = node.entry_id
    return nodes


def _cancel_source_call(
    result: SessionMessageEntry,
    message_entries: dict[str, SessionMessageEntry],
    tool_call_id: str,
) -> tuple[SessionMessageEntry, ToolCall] | None:
    # 来源助手沿真实父链定位：同运行压缩后复用同名调用 ID 时，每份结果绑定自己的发起节点。
    parent_id = result.parent_id
    visited: set[str] = set()
    while parent_id is not None and parent_id not in visited:
        visited.add(parent_id)
        parent = message_entries.get(parent_id)
        if parent is None:
            return None
        blocks = (
            parent.messages[0].content
            if isinstance(parent.messages[0], AssistantMessage)
            else []
        )
        for block in blocks:
            if (
                isinstance(block, ToolCall)
                and block.id == tool_call_id
                and block.name == "cancel_pending_proposal"
            ):
                return parent, block
        parent_id = parent.parent_id
    return None


async def read_cancel_evidence(
    queries: SessionQueries, session_ids: Sequence[str]
) -> list[CancelEvidence]:
    # 取消身份由（会话, 来源助手节点, 调用 ID）复合键保留每份独立结果：
    # 来源助手、工具结果与目标提案必须同属一次真实调用，跨会话或压缩后复用的同名调用各自成立。
    evidence: dict[tuple[str, str, str], CancelEvidence] = {}
    for session_id in session_ids:
        entries = await queries.list_entries(session_id)
        runs = {run.id: run for run in await queries.list_runs(session_id)}
        message_entries = {
            entry.id: entry
            for entry in entries
            if isinstance(entry, SessionMessageEntry)
        }
        for entry in entries:
            if not isinstance(entry, SessionMessageEntry) or entry.run_id is None:
                continue
            message = entry.messages[0]
            if not isinstance(message, ToolResultMessage):
                continue
            if message.tool_name != "cancel_pending_proposal" or message.is_error:
                continue
            try:
                payload = json.loads(text_projection(message.content))
                cancellation = PendingProposalCancellation.model_validate(payload)
            except Exception:
                continue
            match = _cancel_source_call(entry, message_entries, message.tool_call_id)
            if match is None:
                continue
            source, call = match
            if source.run_id != entry.run_id:
                continue
            if (
                call.arguments.get("business_kind") != cancellation.business_kind
                or call.arguments.get("proposal_id") != cancellation.proposal_id
            ):
                continue
            run = runs.get(entry.run_id)
            if run is None:
                continue
            evidence[(session_id, source.id, message.tool_call_id)] = CancelEvidence(
                tool_call_id=message.tool_call_id,
                business_kind=cancellation.business_kind,
                proposal_id=cancellation.proposal_id,
                result=cancellation.model_dump(mode="json"),
                session_id=session_id,
                run_id=entry.run_id,
                request_entry_id=run.request_entry_id,
                source_entry_id=source.id,
                result_entry_id=entry.id,
            )
    return list(evidence.values())


class ContextAmbiguousError(ContextMappingError):
    """模型投影中的正文无法绑定到唯一真实节点身份，禁止据此评分。"""


def _is_user_projectable(node: MessageNode) -> bool:
    # 投影角色决定可绑定性：业务资料与压缩摘要都以 user 角色进入模型请求。
    return node.role == "user"


class _NodeSequence:
    # 按会话时间顺序消费真实节点：来源与身份由节点决定，正文相同不改变判定。
    def __init__(self, nodes: Sequence[MessageNode]) -> None:
        self._user = [node for node in nodes if _is_user_projectable(node)]
        self._assistant = [
            node
            for node in nodes
            if node.kind == "message" and node.role == "assistant" and node.content.strip()
        ]
        self._tool_only_assistants = [
            node
            for node in nodes
            if node.kind == "message" and node.role == "assistant" and not node.content.strip()
        ]

    def take_user(self, text: str) -> MessageNode | None:
        for index, node in enumerate(self._user):
            if node.content == text:
                return self._user.pop(index)
        return None

    def take_assistant(self, text: str) -> MessageNode | None:
        for index, node in enumerate(self._assistant):
            if node.content == text:
                return self._assistant.pop(index)
        return None

    def take_tool_only_assistant(self) -> MessageNode | None:
        if not self._tool_only_assistants:
            return None
        return self._tool_only_assistants.pop(0)


def current_input_entry_ids(
    nodes: Sequence[MessageNode], run_id: str, request_entry_id: str | None
) -> set[str]:
    # 当前输入只由真实节点身份确定：本次运行的请求节点与运行期间消费的 Steering 节点。
    ids = {node.entry_id for node in nodes if node.run_id == run_id and node.role == "user"}
    if request_entry_id is not None:
        ids.add(request_entry_id)
    return ids


def context_items_from_request(
    request: dict,
    *,
    api: str,
    session_id: NonEmptyText,
    current_entry_ids: set[str],
    nodes: Sequence[MessageNode],
    tool_nodes: Sequence[ToolNode],
) -> ContextMapping:
    """按真实消息节点与协议投影建立模型输入来源；缺口逐条落盘。"""
    projection = request_context_projection(request)
    pool = _NodeSequence([
        node for node in nodes if node.session_id == session_id
    ])
    tools_by_id: dict[str, list[ToolNode]] = {}
    tool_identity_map: dict[str, str] = {}
    for node in tool_nodes:
        if node.session_id != session_id:
            continue
        visible = normalize_tool_id(node.tool_call_id, api)
        tools_by_id.setdefault(visible, []).append(node)
        if node.tool_call_id != visible:
            tools_by_id.setdefault(node.tool_call_id, []).append(node)
        tool_identity_map[node.tool_call_id] = visible
    items: list[ContextItem] = []
    gaps: list[ContextGap] = []
    for event in projection.events:
        if isinstance(event, ContextGap):
            gaps.append(event)
            continue
        if event.kind == "system":
            items.append(ContextItem(
                source="system_prompt",
                content=event.content,
                fact_origin="system",
                message_role="system",
            ))
            continue
        if event.kind == "tool":
            item = _tool_item_for(
                event.tool_call_id or "",
                event.content,
                event.tool_name,
                tools_by_id,
                gaps,
            )
            if item is not None:
                items.append(item)
            continue
        if event.kind == "user":
            _append_user_item(
                items, gaps, event.content, current_entry_ids, pool
            )
            continue
        if event.kind == "assistant":
            _append_assistant_item(items, gaps, event.content, pool)
            continue
        if event.kind == "assistant_tool_only":
            node = pool.take_tool_only_assistant()
            gaps.append(ContextGap(
                reason="助手工具调用消息没有可表达的文本内容",
                role="assistant",
                message_entry_id=None if node is None else node.entry_id,
                content_sha256=sha256_text(event.content),
            ))
    return ContextMapping(
        items=items,
        gaps=gaps,
        tool_identity_map=tool_identity_map,
    )


def _append_assistant_item(
    items: list[ContextItem],
    gaps: list[ContextGap],
    text: str,
    pool: _NodeSequence,
) -> None:
    node = pool.take_assistant(text)
    if node is None:
        gaps.append(ContextGap(
            reason="助手正文未匹配到真实消息节点身份",
            role="assistant",
            content_sha256=sha256_text(text),
            ambiguous=True,
        ))
        return
    items.append(ContextItem(
        source="assistant_message",
        content=text,
        fact_origin="assistant",
        message_entry_id=node.entry_id,
        message_role="assistant",
    ))


def _append_user_item(
    items: list[ContextItem],
    gaps: list[ContextGap],
    text: str,
    current_entry_ids: set[str],
    pool: _NodeSequence,
) -> None:
    node = pool.take_user(text)
    if node is None:
        gaps.append(ContextGap(
            reason="用户正文未匹配到真实消息节点身份",
            role="user",
            content_sha256=sha256_text(text),
            ambiguous=True,
        ))
        return
    if node.kind == "business_context":
        items.append(ContextItem(
            source="business_projection",
            content=text,
            fact_origin="system",
            message_entry_id=node.entry_id,
            message_role="user",
        ))
        return
    if node.kind == "compaction":
        # 压缩摘要以 user 角色进入投影：身份属于摘要节点，正文只作为系统事实计分。
        gaps.append(ContextGap(
            reason="压缩摘要以用户角色进入投影，按系统事实计分",
            role="compaction",
            message_entry_id=node.entry_id,
            content_sha256=sha256_text(text),
        ))
        items.append(ContextItem(
            source="prior_message",
            content=text,
            fact_origin="system",
            message_entry_id=node.entry_id,
            message_role="user",
        ))
        return
    if BUSINESS_CONTEXT_TAG in text:
        gaps.append(ContextGap(
            reason="用户正文包含 business_context 标签但未对应真实业务投影节点",
            role="user",
            message_entry_id=node.entry_id,
            content_sha256=sha256_text(text),
        ))
    if node.entry_id in current_entry_ids:
        source: ContextSource = "attachment" if node.has_attachments else "user_message"
    else:
        source = "attachment" if node.has_attachments else "prior_message"
    items.append(ContextItem(
        source=source,
        content=text,
        fact_origin="user",
        message_entry_id=node.entry_id,
        message_role="user",
    ))


def _tool_item_for(
    tool_call_id: str,
    text: str,
    request_tool_name: str | None,
    tools_by_id: dict[str, list[ToolNode]],
    gaps: list[ContextGap],
) -> ContextItem | None:
    if not tool_call_id:
        return None
    nodes = tools_by_id.get(tool_call_id, [])
    if not nodes:
        raise ContextMappingError(
            f"工具结果缺少持久化原文: {tool_call_id}"
        )
    distinct = sorted({node.entry_id for node in nodes})
    if len(distinct) > 1:
        gaps.append(ContextGap(
            reason="同一工具调用 ID 存在多份持久化结果，无法绑定唯一身份: "
            + ",".join(distinct),
            role="tool",
            tool_call_id=tool_call_id,
            content_sha256=sha256_text(text),
            ambiguous=True,
        ))
        return None
    node = nodes[0]
    tool_name = request_tool_name or node.tool_name
    if not text.strip():
        raise ContextMappingError(f"工具结果可见正文为空: {tool_call_id}")
    if text == node.content:
        return ContextItem(
            source="tool_result",
            content=text,
            fact_origin="tool",
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
    if node.content.startswith(text):
        return ContextItem(
            source="tool_result",
            content=text,
            fact_origin="tool",
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            truncated=True,
            omitted_chars=len(node.content) - len(text),
            original_chars=len(node.content),
        )
    raise ContextMappingError(
        f"模型可见工具正文与持久化原文冲突: {tool_call_id}"
    )


def search_evidence_from_tool(
    text: str, tool_call_id: str
) -> SearchResponseEvidence:
    payload = json.loads(text)
    if set(payload) != {"items", "has_more"}:
        raise ValueError("搜索工具响应必须仅包含 items 与 has_more")
    items = [
        VisibleExercise(
            exercise_id=item["id"],
            name=item["name"],
            equipment=item["equipment"],
            target=item["target"],
            matches=list(item.get("matches") or []),
        )
        for item in payload["items"]
    ]
    visible = ModelVisibleSearch(items=items, has_more=payload["has_more"])
    return SearchResponseEvidence(
        query_id=f"tool-call:{tool_call_id}",
        response_json=payload,
        model_visible=visible,
    )


def parse_timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("initial_state.now 必须携带时区")
    return int(parsed.timestamp() * 1000)


def event_target_for(event: dict) -> EventTarget | None:
    target = event.get("target")
    if target is None:
        return None
    return EventTarget(
        business_kind=target["business_kind"],
        proposal_ref=target["proposal_ref"],
        display_ref=target["display_ref"],
        confirmation_ref=target["confirmation_ref"],
    )


def model_identity_summary(model) -> dict:
    return {
        "api": model.api,
        "provider": model.provider,
        "model": model.model,
        "base_url": sanitize_endpoint(model.base_url),
    }


def sanitize_error_text(value: str) -> str:
    """错误产物中的地址统一丢弃 userinfo、query 与 fragment。"""
    def replace(match: re.Match[str]) -> str:
        return sanitize_endpoint(match.group(0))

    return re.sub(r"https?://[^\s\"'<>]+", replace, value)


def message_visible_text(message: Message) -> str:
    if isinstance(message, AssistantMessage):
        return "".join(
            block.text for block in message.content if isinstance(block, TextContent)
        )
    return text_projection(message.content)


def structured_answer(message: AssistantMessage) -> dict:
    return {
        "stop_reason": message.stop_reason,
        "content": [block.model_dump(mode="json") for block in message.content],
        "usage": None if message.usage is None else message.usage.model_dump(mode="json"),
    }
