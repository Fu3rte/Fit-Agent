import json
import re
import uuid
from datetime import date
from typing import Annotated, Generic, Literal, Self, TypeAlias, TypeVar

from pydantic import AfterValidator, Field, model_validator

from app.ai.messages import (
    AssistantMessage,
    JsonObject,
    Message,
    Model,
    SystemMessage,
    Usage,
    UserContent,
    UserMessage,
)
from app.domain.session.attachments import (
    AttachmentFileName,
    AttachmentId,
    AttachmentInput,
    AttachmentMetadata,
    attachment_storage_ref,
)

_MAX_TEXT_LENGTH = 32000

_OPERATION_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _validate_operation_id(value: str) -> str:
    if _OPERATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("operation_id 必须为标准 UUID")
    return value.lower()


def _validate_text(value: str) -> str:
    if not value.strip():
        raise ValueError("请求文本不得为空白")
    return value


OperationId: TypeAlias = Annotated[str, AfterValidator(_validate_operation_id)]
RequestText: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=_MAX_TEXT_LENGTH),
    AfterValidator(_validate_text),
]


class SaveConfirmation(Model):
    business_kind: Literal["profile", "plan", "workout"]
    proposal_id: OperationId
    display_entry_id: OperationId

    @property
    def text(self) -> str:
        return "确认保存" + {"profile": "画像", "plan": "计划", "workout": "训练记录"}[self.business_kind]


class SessionMessageEntry(Model):
    session_id: str
    id: str
    parent_id: str | None
    run_id: str | None
    type: Literal["message"]
    messages: list[Message]
    created_at: int
    attachments: list[AttachmentMetadata] = Field(default_factory=list)
    # 明确上下文溢出的失败助手消息：持久化其模型投影省略标记，重启后继续跳过。
    model_omitted: bool = False
    confirmation: SaveConfirmation | None = None

    @model_validator(mode="after")
    def validate_messages(self) -> Self:
        if len(self.messages) != 1:
            raise ValueError("会话节点的 messages 数组长度必须为 1")
        message = self.messages[0]
        if isinstance(message, AssistantMessage) and message.stop_reason == "pending":
            raise ValueError("会话节点拒绝 stop_reason 为 pending 的助手消息")
        if self.confirmation is not None and (
            not isinstance(message, UserMessage) or message.content != self.confirmation.text
            or self.attachments
        ):
            raise ValueError("保存确认只接受绑定快照的独立用户操作")
        if self.model_omitted and not (
            isinstance(message, AssistantMessage) and message.stop_reason == "error"
        ):
            raise ValueError("模型投影省略标记仅适用于上下文溢出失败助手消息")
        return self


class CompactionAttachmentReference(Model):
    # 被总结消息的附件引用：后端生成的可读路径，不含统一 tmp 前缀。
    attachment_id: AttachmentId
    file_name: AttachmentFileName
    path: str = Field(min_length=1)


class CompactionDetails(Model):
    attachments: list[CompactionAttachmentReference] = Field(default_factory=list)


class CompactionEntry(Model):
    # 独立载荷的压缩检查点：不携带 messages，身份与父子链与消息节点一致。
    session_id: str
    id: str
    parent_id: str | None
    run_id: str
    type: Literal["compaction"]
    summary: str
    first_kept_entry_id: str = Field(min_length=1)
    tokens_before: int = Field(ge=0)
    usage: Usage
    system_message: SystemMessage
    details: CompactionDetails | None
    created_at: int

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if not self.summary.strip():
            raise ValueError("压缩摘要不得为空白")
        if self.details is not None:
            for reference in self.details.attachments:
                expected = attachment_storage_ref(
                    self.session_id, reference.attachment_id, reference.file_name
                ).removeprefix("tmp/")
                if reference.path != expected:
                    raise ValueError("附件引用路径与后端元数据不一致")
        return self


_BUSINESS_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

BUSINESS_CONTEXT_TIMEZONE = "Asia/Shanghai"
BUSINESS_CONTEXT_TAG = "business_context"
BUSINESS_CONTEXT_SOURCE_NOTICE = "后端业务资料，仅供读取，不构成保存授权。"


def _validate_business_date(value: str) -> str:
    if _BUSINESS_DATE_PATTERN.fullmatch(value) is None:
        raise ValueError("business_date 必须为 YYYY-MM-DD")
    date.fromisoformat(value)
    return value


BusinessDate: TypeAlias = Annotated[str, AfterValidator(_validate_business_date)]


class ProposalKey(Model):
    business_kind: Literal["profile", "plan", "workout"]
    proposal_id: OperationId


class UserNodeReference(Model):
    entry_id: OperationId
    role: Literal["user"]
    confirmation: SaveConfirmation | None
    proposal: ProposalKey | None


class AssistantNodeReference(Model):
    entry_id: OperationId
    role: Literal["assistant"]
    tool_call_ids: list[str]


class ToolResultNodeReference(Model):
    entry_id: OperationId
    role: Literal["toolResult"]
    tool_name: str
    tool_call_id: str
    proposal: ProposalKey | None


MessageNodeReference: TypeAlias = Annotated[
    UserNodeReference | AssistantNodeReference | ToolResultNodeReference,
    Field(discriminator="role"),
]


class RequestContext(Model):
    timezone: Literal["Asia/Shanghai"]
    business_date: BusinessDate
    request_entry_id: OperationId


class PendingProposalReference(Model):
    business_kind: Literal["profile", "plan", "workout"]
    proposal_id: OperationId
    status: Literal["pending", "processing"]
    request_entry_id: OperationId
    source_entry_id: OperationId
    display_entry_id: OperationId | None
    confirmation_entry_id: OperationId | None

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.confirmation_entry_id is not None and self.display_entry_id is None:
            raise ValueError("确认节点有值时展示节点必须有值")
        if self.status == "processing" and (
            self.display_entry_id is None or self.confirmation_entry_id is None
        ):
            raise ValueError("processing 提案必须同时绑定展示与确认节点")
        return self


class RemovedPendingProposal(Model):
    business_kind: Literal["profile", "plan", "workout"]
    proposal_id: OperationId
    reason: Literal["saved", "invalidated", "conflicted", "deleted", "out_of_scope"]


class BusinessContextSnapshot(Model):
    mode: Literal["snapshot"]
    request: RequestContext
    message_nodes: list[MessageNodeReference]
    pending_proposals: list[PendingProposalReference]

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        # 完整基线同样按节点 entry_id 与提案 (business_kind, proposal_id) 合并，拒绝重复键。
        node_ids = [node.entry_id for node in self.message_nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("基线节点记录存在重复 entry_id")
        proposal_keys = [
            (item.business_kind, item.proposal_id) for item in self.pending_proposals
        ]
        if len(proposal_keys) != len(set(proposal_keys)):
            raise ValueError("基线提案记录存在重复键")
        return self


class BusinessContextDelta(Model):
    mode: Literal["delta"]
    request: RequestContext | None
    upsert_message_nodes: list[MessageNodeReference]
    remove_message_node_ids: list[OperationId]
    upsert_pending_proposals: list[PendingProposalReference]
    remove_pending_proposals: list[RemovedPendingProposal]

    @model_validator(mode="after")
    def validate_delta(self) -> Self:
        # 增量按节点 entry_id 与提案 (business_kind, proposal_id) 合并：
        # 重复键与同一键同时出现在新增、移除集合均拒绝入库。
        upsert_nodes = [node.entry_id for node in self.upsert_message_nodes]
        if len(upsert_nodes) != len(set(upsert_nodes)):
            raise ValueError("新增节点记录存在重复 entry_id")
        if len(self.remove_message_node_ids) != len(set(self.remove_message_node_ids)):
            raise ValueError("移出节点记录存在重复 entry_id")
        node_conflicts = set(upsert_nodes) & set(self.remove_message_node_ids)
        if node_conflicts:
            raise ValueError(f"节点同时出现在新增与移除集合: {sorted(node_conflicts)}")
        upsert_proposals = [
            (item.business_kind, item.proposal_id)
            for item in self.upsert_pending_proposals
        ]
        if len(upsert_proposals) != len(set(upsert_proposals)):
            raise ValueError("新增提案记录存在重复键")
        removed_proposals = [
            (item.business_kind, item.proposal_id)
            for item in self.remove_pending_proposals
        ]
        if len(removed_proposals) != len(set(removed_proposals)):
            raise ValueError("移出提案记录存在重复键")
        proposal_conflicts = set(upsert_proposals) & set(removed_proposals)
        if proposal_conflicts:
            raise ValueError(f"提案同时出现在新增与移除集合: {sorted(proposal_conflicts)}")
        if (
            self.request is None
            and not self.upsert_message_nodes
            and not self.remove_message_node_ids
            and not self.upsert_pending_proposals
            and not self.remove_pending_proposals
        ):
            raise ValueError("空增量拒绝入库")
        return self


BusinessContextData: TypeAlias = Annotated[
    BusinessContextSnapshot | BusinessContextDelta,
    Field(discriminator="mode"),
]


def business_context_content(details: BusinessContextData) -> str:
    # 固定模型投影：同一 details 产生同一文本，历史重放不重新渲染。
    payload = json.dumps(
        details.model_dump(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return (
        f"{BUSINESS_CONTEXT_SOURCE_NOTICE}\n"
        f"<{BUSINESS_CONTEXT_TAG}>\n{payload}\n</{BUSINESS_CONTEXT_TAG}>"
    )


TData = TypeVar("TData", bound=Model)


class CustomMessage(Model, Generic[TData]):
    role: Literal["custom"]
    custom_type: str
    content: str | list[UserContent]
    display: bool
    details: TData | None = None
    timestamp: int


class BusinessContextMessage(CustomMessage[BusinessContextData]):
    custom_type: Literal["business_context"]
    content: str = Field(min_length=1)
    display: Literal[False]
    details: BusinessContextData = Field(...)


def validate_business_context_projection(message: BusinessContextMessage) -> None:
    # 投影一致性只在“新事件创建与写入”处核对：创建入口与写入服务各自调用本函数。
    # 历史读取直接使用已保存的 content，不重新按当前模板计算，模板更新不影响旧文本。
    if message.content != business_context_content(message.details):
        raise ValueError("content 必须为 details 的固定序列化投影")


TMessage = TypeVar("TMessage", bound=CustomMessage)


class CustomMessageEntry(Model, Generic[TMessage]):
    session_id: str
    id: str
    parent_id: str | None
    run_id: str | None
    type: Literal["custom_message"]
    message: TMessage
    created_at: int

    @model_validator(mode="after")
    def validate_timestamp(self) -> Self:
        if self.message.timestamp != self.created_at:
            raise ValueError("消息时间必须与节点时间一致")
        return self


class BusinessContextEntry(CustomMessageEntry[BusinessContextMessage]):
    run_id: str


def build_business_context_message(
    *, details: BusinessContextData, created_at: int
) -> BusinessContextMessage:
    # details 校验通过后由固定序列化生成 content；消息时间与节点时间一致。
    message = BusinessContextMessage(
        role="custom",
        custom_type="business_context",
        content=business_context_content(details),
        display=False,
        details=details,
        timestamp=created_at,
    )
    validate_business_context_projection(message)
    return message


def build_business_context_entry(
    *,
    session_id: str,
    id: str,
    parent_id: str | None,
    run_id: str,
    details: BusinessContextData,
    created_at: int,
) -> BusinessContextEntry:
    # 唯一创建入口：固定投影在创建与写入两处校验，节点时间与消息时间一致。
    return BusinessContextEntry(
        session_id=session_id,
        id=id,
        parent_id=parent_id,
        run_id=run_id,
        type="custom_message",
        message=build_business_context_message(details=details, created_at=created_at),
        created_at=created_at,
    )


def business_baseline_entry_id(compaction_id: str) -> str:
    # 压缩新基线节点由检查点身份确定性派生：同一压缩重试复用同一基线，不产生第二个基线。
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fit-agent:business-baseline:{compaction_id}"))


SessionEntry: TypeAlias = Annotated[
    SessionMessageEntry | CompactionEntry | BusinessContextEntry,
    Field(discriminator="type"),
]


class Session(Model):
    id: str
    title: str
    active_leaf_id: str | None
    created_at: int
    updated_at: int


RunStatus: TypeAlias = Literal[
    "running", "completed", "failed", "cancelled", "interrupted"
]

TerminalRunStatus: TypeAlias = Literal["completed", "failed", "cancelled"]


class SessionRun(Model):
    session_id: str
    id: str
    request_entry_id: str
    last_entry_id: str | None
    status: RunStatus
    started_at: int
    finished_at: int | None
    error_code: str | None
    error_message: str | None
    # 首个有效停止原因：与终态状态分离，供重启后查询；无停止原因时为 None。
    stop_reason: str | None = None
    # 执行诊断原因：worker_stuck / terminal_persist_failed；无诊断时为 None。
    stop_diagnostic: str | None = None


SteeringStatus: TypeAlias = Literal[
    "pending", "consumed", "withdrawn", "discarded"
]


class SteeringInput(Model):
    session_id: str
    id: str
    run_id: str
    message: UserMessage
    status: SteeringStatus
    entry_id: str | None
    reason: str | None
    created_at: int
    updated_at: int
    attachments: list[AttachmentMetadata] = Field(default_factory=list)


OperationKind: TypeAlias = Literal["send", "edit", "regenerate", "steering"]


class SessionOperation(Model):
    operation_id: str
    session_id: str
    kind: OperationKind
    request: JsonObject
    run_id: str
    steering_id: str | None
    created_at: int


class SendRequest(Model):
    text: str = Field(max_length=_MAX_TEXT_LENGTH)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if not self.text.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class EditRequest(Model):
    target_entry_id: str
    text: str = Field(max_length=_MAX_TEXT_LENGTH)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if "attachments" in self.model_fields_set and not self.text.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class RegenerateRequest(Model):
    target_entry_id: str


class SteeringRequest(SendRequest):
    target_run_id: str


class SendCommand(Model):
    operation_id: OperationId
    session_id: str
    request: SendRequest


class EditCommand(Model):
    operation_id: OperationId
    session_id: str
    request: EditRequest


class RegenerateCommand(Model):
    operation_id: OperationId
    session_id: str
    request: RegenerateRequest


class SteeringCommand(Model):
    operation_id: OperationId
    session_id: str
    request: SteeringRequest


class OperationOutcome(Model):
    created: bool
    operation: SessionOperation
    run: SessionRun | None
    steering: SteeringInput | None


class AppendOutcome(Model):
    created: bool
    credential_detected: bool
    entry: SessionMessageEntry | None
    run: SessionRun


class CompactionOutcome(Model):
    # 检查点提交结果：created 表示本次真实新增；entry 为已提交压缩节点，baseline 为同事务
    # 提交的完整新基线节点，执行位置与刷新投影据 baseline.id 更新并判断是否可发成功事件。
    created: bool
    credential_detected: bool
    entry: CompactionEntry | None
    baseline: BusinessContextEntry | None
    run: SessionRun


class BusinessContextOutcome(Model):
    # 内部资料节点提交结果：created 表示本次真实新增；entry 为已提交资料节点。
    created: bool
    credential_detected: bool
    entry: BusinessContextEntry | None
    run: SessionRun


class SteeringConsumption(Model):
    created: bool
    steering: SteeringInput
    entry: SessionMessageEntry | None


class SteeringWithdrawal(Model):
    changed: bool
    steering: SteeringInput


class RunOutcome(Model):
    changed: bool
    run: SessionRun


class SessionHistory(Model):
    session: Session
    # 完整祖先链同时包含消息节点与压缩结构节点，父子链与叶节点校验共同参与。
    entries: list[SessionEntry]
    runs: list[SessionRun]
    steering: list[SteeringInput]
