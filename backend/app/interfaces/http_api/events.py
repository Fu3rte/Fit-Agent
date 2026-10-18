from collections.abc import Callable

from app.agent.tool import CredentialDetectedError
from app.ai.messages import ThinkingContent
from app.application.session.credentials import CredentialFilter
from app.interfaces.http_api.projections import (
    public_content,
    public_text_visible,
    public_tool_execution_end,
    public_tool_execution_update,
)


class HttpRunEvents:
    def __init__(
        self,
        publish: Callable[[str, dict], None],
        guard: CredentialFilter,
        parents: dict[str, str],
        record_first_text: Callable[[], None],
    ) -> None:
        self._publish = publish
        self._guard = guard
        # 与执行提交回调共享同一个字典，提交成功后更新身份。
        self._parents = parents
        self._record_first_text = record_first_text

    def custom_message(self, entry) -> None:
        data = {
            "type": "custom_message",
            "entry_id": entry.id,
            "parent_id": entry.parent_id,
            "run_id": entry.run_id,
            "created_at": entry.created_at,
        }
        if self._guard.contains(data):
            raise CredentialDetectedError()
        self._publish("custom_message", data)

    async def emit(self, event) -> None:
        kind = event["type"]
        if kind in {"message_start", "message_update", "message_end"}:
            message = event["message"]
            if self._guard.contains(message.model_dump()):
                raise CredentialDetectedError()
            data = {
                "message_id": event["message_id"],
                "content": public_content(message, self._guard, kind != "message_end"),
            }
            if kind == "message_update":
                update = event["assistant_event"]
                index = update["content_index"]
                block = message.content[index]
                if isinstance(block, ThinkingContent) and block.redacted:
                    return
                data.update(content_index=index, update_type=update["type"])
            elif kind == "message_end":
                data["entry_id"] = event["message_id"]
                data["parent_id"] = self._parents.get(event["message_id"])
                data["stop_reason"] = message.stop_reason
            if public_text_visible(data["content"]):
                self._record_first_text()
            self._publish(kind, data)
        elif kind == "tool_start":
            data = {
                "tool_call_id": event["tool_call_id"],
                "name": event["name"],
                "arguments": event["arguments"],
            }
            if self._guard.contains(data):
                raise CredentialDetectedError()
            self._publish(kind, data)
        elif kind == "tool_result":
            data = {
                "tool_call_id": event["tool_call_id"],
                "content": event["content"],
                "is_error": event["is_error"],
                "entry_id": event["message_id"],
                "parent_id": self._parents.get(event["message_id"]),
            }
            if self._guard.contains(data):
                raise CredentialDetectedError()
            self._publish(kind, data)
        elif kind == "tool_execution_end":
            data = public_tool_execution_end(event)
            if self._guard.contains(data):
                raise CredentialDetectedError()
            self._publish(kind, data)
        elif kind == "compaction_start":
            self._publish(kind, {
                "compaction_id": event["compaction_id"],
                "reason": event["reason"],
            })
        elif kind == "compaction_end":
            self._publish(kind, {
                "compaction_id": event["compaction_id"],
                "entry_id": event["entry_id"],
                "parent_id": event["parent_id"],
            })
        elif kind == "custom_message":
            data = {
                "type": "custom_message",
                "entry_id": event["entry_id"],
                "parent_id": event["parent_id"],
                "run_id": event["run_id"],
                "created_at": event["created_at"],
            }
            if self._guard.contains(data):
                raise CredentialDetectedError()
            self._publish(kind, data)

    def tool_update(self, tool_call_id: str, tool_name: str, result) -> None:
        # 凭据在 harness 通知前拦截，进度快照沿用工具结果文本投影。
        self._publish(
            "tool_execution_update",
            public_tool_execution_update(
                tool_call_id, tool_name, result.content, result.is_error
            ),
        )
