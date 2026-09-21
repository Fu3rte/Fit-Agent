"""会话 API：列出／新建／详情／删除，详情重建前端可直接渲染的轮次（阶段 5）。"""

from typing import Any

from fastapi import APIRouter, Request

from app.api.dependencies import app_services, iso_now
from app.api.errors import InvalidRequestShape
from app.api.schemas.conversation_dto import (
    ConversationCreateBody,
    conversation_dto,
)
from app.application.ports import ConversationNotFound
from app.domain.conversations.context import build_conversation_display

router = APIRouter()

#: 不存在与非法身份共用同一可见文本：调用方无法据响应区分两者。
_MISSING_MESSAGE = "会话不存在或身份非法"


@router.get("/api/conversations")
async def list_conversations(request: Request) -> dict[str, Any]:
    """全部会话，按 ``updated_at`` 降序。"""
    conversations = await app_services(request).conversations.list_conversations()
    return {"conversations": [conversation_dto(item) for item in conversations]}


@router.post("/api/conversations", status_code=201)
async def create_conversation(
    body: ConversationCreateBody, request: Request
) -> dict[str, Any]:
    """新建空会话：身份由服务端生成，标题空白即标准 400，不落入存储层异常。"""
    title = body.title.strip()
    if not title:
        raise InvalidRequestShape("会话标题不能为空")
    created = await app_services(request).conversations.create_conversation(
        title=title, created_at=iso_now()
    )
    return conversation_dto(created)


@router.get("/api/conversations/{conversation_id}")
async def read_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    """会话详情：会话全部 Entry 的轮次与压缩分隔一次返回，不泄漏内部 Prompt 与异常。"""
    detail = await app_services(request).conversations.read_conversation_detail(
        conversation_id
    )
    if detail is None:
        raise ConversationNotFound(_MISSING_MESSAGE)
    return {
        "conversation": conversation_dto(detail.conversation),
        **build_conversation_display(detail.entries, detail.runs, detail.events),
    }


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    """删除会话：Entry／Run／Event 由外键级联清理；不存在与非法身份返回同一错误。"""
    deleted = await app_services(request).conversations.delete_conversation(
        conversation_id
    )
    if not deleted:
        raise ConversationNotFound(_MISSING_MESSAGE)
    return {"deleted": True}
