import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

EntryType = Literal["message", "compaction", "confirmation"]
MessageRole = Literal["user", "assistant"]
MessageStatus = Literal["complete", "partial", "failed", "aborted"]
ConfirmationAction = Literal["plan_confirmed", "plan_rejected", "workout_confirmed"]
RunStatus = Literal["pending", "running", "waiting", "completed", "failed", "cancelled"]

#: 运行期闭集由 Literal 别名派生：校验取值域只有一处定义，改类型即改校验。
ENTRY_TYPES: tuple[EntryType, ...] = get_args(EntryType)
MESSAGE_ROLES: tuple[MessageRole, ...] = get_args(MessageRole)
MESSAGE_STATUSES: tuple[MessageStatus, ...] = get_args(MessageStatus)
CONFIRMATION_ACTIONS: tuple[ConfirmationAction, ...] = get_args(ConfirmationAction)
RUN_STATUSES: tuple[RunStatus, ...] = get_args(RunStatus)

_MESSAGE_KEYS = frozenset({"role", "content", "status", "run_id", "usage", "provider", "model"})
_COMPACTION_KEYS = frozenset(
    {"summary", "first_kept_entry_id", "tokens_before", "usage"}
)
_CONFIRMATION_KEYS = frozenset(
    {"action", "run_id", "draft_plan_id", "workout_session_id", "text"}
)


class InvalidConversationPayload(ValueError):
    """payload 形状不符合已拍闭集：写前拒绝，禁止落库。"""


class InvalidConversationRow(ValueError):
    """数据库行无法解析：数据损坏，大声失败不静默兜底。"""


def _require_text(
    payload: Mapping[str, Any],
    key: str,
    error_cls: type[ValueError] = InvalidConversationPayload,
) -> str:
    return require_text(payload.get(key), f"payload.{key}", error_cls)


def _reject_unknown(
    payload: Mapping[str, Any],
    allowed: frozenset[str],
    error_cls: type[ValueError] = InvalidConversationPayload,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise error_cls(f"payload 含未定义字段：{unknown}")


def _optional_text(
    payload: Mapping[str, Any],
    key: str,
    error_cls: type[ValueError] = InvalidConversationPayload,
) -> None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise error_cls(f"payload.{key} 必须是字符串：{value!r}")


def _validate_message(
    payload: Mapping[str, Any], error_cls: type[ValueError] = InvalidConversationPayload
) -> None:
    _reject_unknown(payload, _MESSAGE_KEYS, error_cls)
    role = _require_text(payload, "role", error_cls)
    if role not in MESSAGE_ROLES:
        raise error_cls(f"message.role 不在二态内：{role!r}")
    _require_text(payload, "content", error_cls)
    status = _require_text(payload, "status", error_cls)
    if status not in MESSAGE_STATUSES:
        raise error_cls(f"message.status 不在四态内：{status!r}")
    _require_text(payload, "run_id", error_cls)
    _optional_text(payload, "provider", error_cls)
    _optional_text(payload, "model", error_cls)
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        raise error_cls(f"message.usage 必须是对象：{usage!r}")


def _validate_compaction(
    payload: Mapping[str, Any], error_cls: type[ValueError] = InvalidConversationPayload
) -> None:
    _reject_unknown(payload, _COMPACTION_KEYS, error_cls)
    _require_text(payload, "summary", error_cls)
    _require_text(payload, "first_kept_entry_id", error_cls)
    tokens_before = payload.get("tokens_before")
    if not isinstance(tokens_before, int) or isinstance(tokens_before, bool):
        raise error_cls(f"compaction.tokens_before 必须是整数：{tokens_before!r}")
    if tokens_before < 0:
        raise error_cls(f"compaction.tokens_before 不得为负：{tokens_before}")
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        raise error_cls(f"compaction.usage 必须是对象：{usage!r}")


def _require_positive_int(
    payload: Mapping[str, Any],
    key: str,
    error_cls: type[ValueError] = InvalidConversationPayload,
) -> None:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise error_cls(f"confirmation.{key} 必须是正整数：{value!r}")


def _forbid(
    payload: Mapping[str, Any],
    key: str,
    action: str,
    error_cls: type[ValueError] = InvalidConversationPayload,
) -> None:
    if payload.get(key) is not None:
        raise error_cls(f"confirmation.{action} 不得携带 {key}：{payload[key]!r}")


def _validate_confirmation(
    payload: Mapping[str, Any], error_cls: type[ValueError] = InvalidConversationPayload
) -> None:
    """确认动作与业务身份严格绑定：三类各有唯一身份字段，不混用、不缺失。"""
    _reject_unknown(payload, _CONFIRMATION_KEYS, error_cls)
    action = _require_text(payload, "action", error_cls)
    if action not in CONFIRMATION_ACTIONS:
        raise error_cls(f"confirmation.action 不在三态内：{action!r}")
    _require_text(payload, "run_id", error_cls)
    _require_text(payload, "text", error_cls)
    if action in ("plan_confirmed", "plan_rejected"):
        _require_positive_int(payload, "draft_plan_id", error_cls)
        _forbid(payload, "workout_session_id", action, error_cls)
    else:
        _require_positive_int(payload, "workout_session_id", error_cls)
        _forbid(payload, "draft_plan_id", action, error_cls)


def validate_payload(
    entry_type: str,
    payload: Mapping[str, Any],
    error_cls: type[ValueError] = InvalidConversationPayload,
) -> dict[str, Any]:
    """按 Entry 类型校验 payload 形状；写路径用 InvalidConversationPayload，读路径传行错误类型。"""
    if not isinstance(payload, Mapping):
        raise error_cls(f"payload 必须是对象：{payload!r}")
    if entry_type == "message":
        _validate_message(payload, error_cls)
    elif entry_type == "compaction":
        _validate_compaction(payload, error_cls)
    elif entry_type == "confirmation":
        _validate_confirmation(payload, error_cls)
    else:
        raise error_cls(f"entry_type 不在三态内：{entry_type!r}")
    return dict(payload)


def load_payload(entry_type: str, payload_json: str) -> dict[str, Any]:
    """读回合法 JSON 并按 Entry 类型校验：形状／闭集损坏抛 :class:`InvalidConversationRow`。

    畸形 JSON 由 ``json.loads`` 原位抛 ``JSONDecodeError``：不捕获、不 fallback。
    """
    decoded = json.loads(payload_json)
    if not isinstance(decoded, dict):
        raise InvalidConversationRow(f"payload_json 顶层必须是对象：{decoded!r}")
    return validate_payload(entry_type, decoded, InvalidConversationRow)


def dump_event_payload(payload: Mapping[str, Any]) -> str:
    """Run Event 载荷序列化：事件形状由产生方与 DTO 负责，这里只保证可序列化对象。"""
    if not isinstance(payload, Mapping):
        raise InvalidConversationPayload(f"event payload 必须是对象：{payload!r}")
    return json.dumps(dict(payload), ensure_ascii=False)


def load_event_payload(payload_json: str) -> dict[str, Any]:
    """读回合法 JSON 的 Run Event 载荷：顶层非对象抛 :class:`InvalidConversationRow`。

    畸形 JSON 由 ``json.loads`` 原位抛 ``JSONDecodeError``：不捕获、不 fallback。
    """
    decoded = json.loads(payload_json)
    if not isinstance(decoded, dict):
        raise InvalidConversationRow(f"event payload_json 顶层必须是对象：{decoded!r}")
    return decoded


def require_text(
    value: Any, what: str, error_cls: type[ValueError] = InvalidConversationPayload
) -> str:
    """校验调用方提供的身份／时间戳：repo 不生成、不补默认值。"""
    if not isinstance(value, str) or not value:
        raise error_cls(f"{what} 必须是非空字符串：{value!r}")
    return value


def _require_closed(
    value: str,
    allowed: tuple[str, ...],
    what: str,
    error_cls: type[ValueError],
) -> Any:
    if value not in allowed:
        raise error_cls(f"{what}不在闭集内：{value!r}")
    return value


def require_run_status(
    value: str, error_cls: type[ValueError] = InvalidConversationPayload
) -> RunStatus:
    """Run 状态闭集校验：写路径越界即 :class:`InvalidConversationPayload`，读回行传入行错误类型。"""
    return cast(RunStatus, _require_closed(value, RUN_STATUSES, "Run 状态", error_cls))


def require_entry_type(
    value: str, error_cls: type[ValueError] = InvalidConversationPayload
) -> EntryType:
    """entry_type 闭集校验：写路径越界即 :class:`InvalidConversationPayload`，读回行传入行错误类型。"""
    return cast(EntryType, _require_closed(value, ENTRY_TYPES, "entry_type", error_cls))


#: 父级批准的单向迁移矩阵：终态不复活，同态更新不在任何后继集合内。
RUN_TRANSITIONS: dict[RunStatus, tuple[RunStatus, ...]] = {
    "pending": ("running", "failed", "cancelled"),
    "running": ("waiting", "completed", "failed", "cancelled"),
    "waiting": ("running", "completed", "failed", "cancelled"),
    "completed": (),
    "failed": (),
    "cancelled": (),
}


def require_run_transition(current: RunStatus, target: RunStatus) -> None:
    """矩阵校验：非法迁移（含同态、终态复活）即 :class:`InvalidConversationPayload`。"""
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidConversationPayload(f"非法 Run 状态迁移：{current} -> {target}")


@dataclass(frozen=True, slots=True)
class Conversation:
    """``conversations`` 一行：标题与两个由调用方提供的时间戳。"""

    id: str
    title: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Conversation":
        return cls(
            id=str(row["id"]),
            title=str(row["title"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class ConversationEntry:
    """``conversation_entries`` 一行：会话内序号与 id／entry_type／payload 三要素。"""

    id: str
    conversation_id: str
    sequence: int
    entry_type: EntryType
    payload: dict[str, Any]
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ConversationEntry":
        entry_type = str(row["entry_type"])
        return cls(
            id=str(row["id"]),
            conversation_id=str(row["conversation_id"]),
            sequence=int(row["sequence"]),
            entry_type=require_entry_type(entry_type, InvalidConversationRow),
            payload=load_payload(entry_type, str(row["payload_json"])),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class ConversationRun:
    """``conversation_runs`` 一行：幂等键、thread 身份、两条 Entry 关联与状态。"""

    id: str
    conversation_id: str
    thread_id: str
    client_request_id: str
    user_entry_id: str
    assistant_entry_id: str | None
    status: RunStatus
    error_code: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ConversationRun":
        assistant = row["assistant_entry_id"]
        error_code = row["error_code"]
        return cls(
            id=str(row["id"]),
            conversation_id=str(row["conversation_id"]),
            thread_id=str(row["thread_id"]),
            client_request_id=str(row["client_request_id"]),
            user_entry_id=str(row["user_entry_id"]),
            assistant_entry_id=None if assistant is None else str(assistant),
            status=require_run_status(str(row["status"]), InvalidConversationRow),
            error_code=None if error_code is None else str(error_code),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class RunEvent:
    """``conversation_run_events`` 一行：同一 Run 内由 sequence 保序的事件。"""

    id: int
    run_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RunEvent":
        return cls(
            id=int(row["id"]),
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            event_type=str(row["event_type"]),
            payload=load_event_payload(str(row["payload_json"])),
            created_at=str(row["created_at"]),
        )
