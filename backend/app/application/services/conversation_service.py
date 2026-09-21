"""conversation 用例编排：会话 CRUD、会话详情聚合与遗留 Run 收敛。"""

from dataclasses import dataclass
from uuid import uuid4

from app.application.ports import Conversations
from app.domain.conversations.schema import (
    Conversation,
    ConversationEntry,
    ConversationRun,
    RunEvent,
)

#: 启动收敛的固定 ``error_code``：上一次进程终止时仍未结束的 Run 以该原因标为 ``failed``。
INTERRUPTED_RUN_ERROR_CODE = "server_restart"


@dataclass(frozen=True, slots=True)
class ConversationDetail:
    """会话详情的领域对象聚合：会话本身、全部 Entry、全部 Run 与它们的全部事件。"""

    conversation: Conversation
    entries: tuple[ConversationEntry, ...]
    runs: tuple[ConversationRun, ...]
    events: tuple[RunEvent, ...]


class ConversationService:
    """会话与 Run 生命周期的用例入口；Agent Run 自身的写入生命周期归 ``agent/run_service.py``。"""

    def __init__(self, conversations: Conversations) -> None:
        self._conversations = conversations

    async def list_conversations(self) -> tuple[Conversation, ...]:
        """全部会话，按 ``updated_at`` 降序。"""
        return await self._conversations.list_conversations()

    async def create_conversation(self, *, title: str, created_at: str) -> Conversation:
        """新建空会话：身份由服务端生成。"""
        return await self._conversations.create_conversation(
            conversation_id=str(uuid4()), title=title, created_at=created_at
        )

    async def read_conversation_detail(
        self, conversation_id: str
    ) -> ConversationDetail | None:
        """会话详情的四类领域对象一次读齐；会话不存在即 None。"""
        conversation = await self._conversations.read_conversation(conversation_id)
        if conversation is None:
            return None
        runs = await self._conversations.list_runs(conversation_id)
        events: list[RunEvent] = []
        for run in runs:
            events.extend(await self._conversations.list_run_events(run.id))
        return ConversationDetail(
            conversation=conversation,
            entries=await self._conversations.list_entries(conversation_id),
            runs=runs,
            events=tuple(events),
        )

    async def delete_conversation(self, conversation_id: str) -> bool:
        """删除会话：Entry／Run／Event 由外键级联清理；返回是否删除了行。"""
        return await self._conversations.delete_conversation(conversation_id)

    async def converge_unfinished_runs(self, *, error_code: str, updated_at: str) -> int:
        """启动收敛：遗留 ``pending``／``running`` Run 一律标为 ``failed``，返回收敛行数。"""
        return await self._conversations.converge_unfinished_runs(
            error_code=error_code, updated_at=updated_at
        )

    async def cancel_active_run(self, run_id: str, *, updated_at: str) -> bool:
        """客户端断开收敛：仍活动的 Run 转 ``cancelled``，返回是否发生迁移。"""
        return await self._conversations.cancel_active_run(run_id, updated_at=updated_at)
