from app.ai.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    text_projection,
)
from app.application.session.credentials import CredentialFilter
from app.domain.business.models import SaveConfirmationOperation
from app.domain.session.models import (
    BusinessContextEntry,
    CompactionEntry,
    SessionEntry,
    SteeringInput,
)

_NO_CREDENTIALS = CredentialFilter(())


def session_object(session) -> dict:
    return {
        "session_id": session.id,
        "title": session.title,
        "active_leaf_id": session.active_leaf_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def run_object(run) -> dict:
    return {
        "session_id": run.session_id,
        "run_id": run.id,
        "request_entry_id": run.request_entry_id,
        "last_entry_id": run.last_entry_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "stop_reason": run.stop_reason,
    }


def steering_object(steering) -> dict:
    return {
        "attachments": [attachment_object(item) for item in steering.attachments],
        "session_id": steering.session_id,
        "run_id": steering.run_id,
        "steering_id": steering.id,
        "status": steering.status,
        "entry_id": steering.entry_id,
        "reason": steering.reason,
        "created_at": steering.created_at,
        "updated_at": steering.updated_at,
    }


def send_result(operation_id: str, run) -> dict:
    return {
        "operation_id": operation_id,
        "session_id": run.session_id,
        "run_id": run.id,
        "request_entry_id": run.request_entry_id,
        "status": run.status,
    }


def pending_reference(reference) -> dict:
    # PendingProposalReference 为 dataclass；按 §7.6 字段白名单投影。
    return {
        "business_kind": reference.business_kind,
        "proposal_id": reference.proposal_id,
        "status": reference.status,
        "request_entry_id": reference.request_entry_id,
        "source_entry_id": reference.source_entry_id,
        "display_entry_id": reference.display_entry_id,
        "confirmation_entry_id": reference.confirmation_entry_id,
    }


def confirmation_operation_object(operation: SaveConfirmationOperation) -> dict:
    # §7.3 公开投影：保留 result 与 error 的必填 null 及业务结果内的合法 null。
    payload = operation.model_dump()
    error = payload["error"]
    if error is not None and error["errors"] is None:
        del error["errors"]
    return payload


def public_content(message: AssistantMessage, secrets: CredentialFilter, truncate: bool) -> list[dict]:
    def text(value: str) -> str:
        return secrets.tail_safe(value) if truncate else value

    content = []
    for index, block in enumerate(message.content):
        if isinstance(block, TextContent):
            content.append({
                "content_index": index,
                "type": "text",
                "text": text(block.text),
            })
        elif isinstance(block, ThinkingContent) and not block.redacted:
            content.append({
                "content_index": index,
                "type": "thinking",
                "thinking": text(block.thinking),
            })
        elif isinstance(block, ToolCall):
            content.append({
                "content_index": index,
                "type": "tool_call",
                "tool_call_id": block.id,
                "name": block.name,
                "arguments": block.arguments,
            })
    return content


def public_text_visible(content: list[dict]) -> bool:
    # 首正文判定以公开投影为准。
    return any(block["type"] == "text" and block["text"] for block in content)


def public_tool_execution_end(event: dict) -> dict:
    return {
        "tool_call_id": event["tool_call_id"],
        "tool_name": event["name"],
        "content": event["content"],
        "is_error": event["is_error"],
    }


def public_tool_execution_update(
    tool_call_id: str, tool_name: str, content: list, is_error: bool
) -> dict:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "content": text_projection(content),
        "is_error": is_error,
    }


def public_message(message: Message) -> dict:
    if isinstance(message, SystemMessage):
        return {"role": "system"}
    if isinstance(message, UserMessage):
        return {
            "role": "user",
            "attachments": [],
            "text": text_projection(message.content),
            "timestamp": message.timestamp,
        }
    if isinstance(message, AssistantMessage):
        return {
            "role": "assistant",
            "content": public_content(message, _NO_CREDENTIALS, False),
            "stop_reason": message.stop_reason,
            "timestamp": message.timestamp,
        }
    if isinstance(message, ToolResultMessage):
        return {
            "role": "toolResult",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": text_projection(message.content),
            "is_error": message.is_error,
            "timestamp": message.timestamp,
        }
    raise ValueError("未知消息角色")


def attachment_object(metadata) -> dict:
    return {
        "attachment_id": metadata.attachment_id, "file_name": metadata.file_name,
        "size_bytes": metadata.size_bytes, "created_at": metadata.created_at,
    }


def history_entry(entry: SessionEntry) -> dict:
    # 隐藏节点只公开身份，摘要、业务资料和内部附件路径保持私有。
    public = {
        "type": entry.type,
        "entry_id": entry.id,
        "parent_id": entry.parent_id,
        "run_id": entry.run_id,
        "created_at": entry.created_at,
    }
    if isinstance(entry, (CompactionEntry, BusinessContextEntry)):
        return public
    message = public_message(entry.messages[0])
    if message["role"] == "user":
        message["attachments"] = [attachment_object(item) for item in entry.attachments]
    return {**public, "message": message}


def history_steering(steering: SteeringInput) -> dict:
    return {
        "session_id": steering.session_id,
        "run_id": steering.run_id,
        "steering_id": steering.id,
        "text": text_projection(steering.message.content),
        "attachments": [attachment_object(item) for item in steering.attachments],
        "timestamp": steering.message.timestamp,
        "status": steering.status,
        "entry_id": steering.entry_id,
        "reason": steering.reason,
        "created_at": steering.created_at,
        "updated_at": steering.updated_at,
    }
