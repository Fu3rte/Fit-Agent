"""会话 API：列出／新建／详情／删除，详情重建前端可直接渲染的轮次（阶段 5）。"""

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request

from api.deps import iso_now
from api.dto import (
    ConversationCreateBody,
    InvalidRequestShape,
    conversation_dto,
)
from domain.conversations.context import build_conversation_display
from domain.conversations.repo import ConversationNotFound, ConversationRepo

router = APIRouter()

#: 不存在与非法身份共用同一可见文本：调用方无法据响应区分两者。
_MISSING_MESSAGE = "会话不存在或身份非法"


@router.get("/api/conversations")
async def list_conversations(request: Request) -> dict[str, Any]:
    """全部会话，按 ``updated_at`` 降序。"""
    conversations = await _repo(request).list_conversations()
    return {"conversations": [conversation_dto(item) for item in conversations]}


@router.post("/api/conversations", status_code=201)
async def create_conversation(
    body: ConversationCreateBody, request: Request
) -> dict[str, Any]:
    """新建空会话：身份由服务端生成，标题空白即标准 400，不落入存储层异常。"""
    title = body.title.strip()
    if not title:
        raise InvalidRequestShape("会话标题不能为空")
    created = await _repo(request).create_conversation(
        conversation_id=str(uuid4()), title=title, created_at=iso_now()
    )
    return conversation_dto(created)


@router.get("/api/conversations/{conversation_id}")
async def read_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    """会话详情：会话全部 Entry 的轮次与压缩分隔一次返回，不泄漏内部 Prompt 与异常。"""
    repo = _repo(request)
    conversation = await repo.read_conversation(conversation_id)
    if conversation is None:
        raise ConversationNotFound(_MISSING_MESSAGE)
    entries = await repo.list_entries(conversation_id)
    runs = await repo.list_runs(conversation_id)
    events = [
        event for run in runs for event in await repo.list_run_events(run.id)
    ]
    return {
        "conversation": conversation_dto(conversation),
        **build_conversation_display(entries, runs, events),
    }


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    """删除会话：Entry／Run／Event 由外键级联清理；不存在与非法身份返回同一错误。"""
    if not await _repo(request).delete_conversation(conversation_id):
        raise ConversationNotFound(_MISSING_MESSAGE)
    return {"deleted": True}


def _repo(request: Request) -> ConversationRepo:
    return ConversationRepo(request.app.state.db)
