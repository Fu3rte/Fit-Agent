"""会话 API 的请求体模型与响应 DTO。"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain.conversations.schema import Conversation


class ConversationCreateBody(BaseModel):
    """``POST /api/conversations`` 的请求体：新建会话的标题由客户端提供。"""

    model_config = ConfigDict(extra="forbid")

    title: str


def conversation_dto(conversation: Conversation) -> dict[str, Any]:
    """会话头 → 传输对象：稳定身份、展示标题与两个时间戳。"""
    return {
        "id": conversation.id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }
