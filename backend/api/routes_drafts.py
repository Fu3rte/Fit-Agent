"""草稿业务接口路由（01 1.2：草稿查询／纠错／确认／丢弃走业务接口，不靠历史通知）。

传输边界（stage2.md §5 S2-07）：路由只校验传输形状、调用应用层、映射结果与错误；领域规则
（revision、业务基线、终态、首次建档完整性）、SQL 与事务编排都在 ``app/drafts.py``／
``app/confirm.py`` 与各 repo。草稿创建只在内部应用层（不开放 HTTP 面），本模块不提供创建
草稿、重算或直接写正式档案的入口。
"""

from typing import Any

from fastapi import APIRouter, Request

from api.dto import (
    commit_result_dto,
    draft_dto,
    json_object_body,
    proposed_profile_from_dto,
    seen_revision_from_dto,
)
from app.confirm import ConfirmService
from app.drafts import DraftService, UnknownDraft

router = APIRouter()

_REVISE_KEYS = frozenset({"revision", "payload"})
_CONFIRM_KEYS = frozenset({"revision"})
_DISCARD_KEYS = frozenset()  # 丢弃请求体不接受任何字段


@router.get("/api/sessions/{session_id}/drafts")
async def list_session_drafts(
    session_id: str, request: Request
) -> list[dict[str, Any]]:
    """该会话已持久化草稿的当前状态（各带结构化 Diff）；不依赖历史通知。"""
    views = await DraftService(request.app.state.db).list_drafts(session_id)
    return [draft_dto(view) for view in views]


@router.get("/api/drafts/{draft_id}")
async def get_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """单草稿当前内容、revision、状态、结构化 Diff 与已提交结果；不存在即明确未找到。"""
    view = await DraftService(request.app.state.db).get_draft(draft_id)
    if view is None:
        raise UnknownDraft(f"草稿不存在：{draft_id}")
    return draft_dto(view)


@router.post("/api/drafts/{draft_id}/revise")
async def revise_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """内容＋所见 revision 纠错 Pending 草稿；只改草稿，不自动提交。"""
    body = await json_object_body(request, keys=_REVISE_KEYS)
    view = await DraftService(request.app.state.db).revise_profile_draft(
        draft_id=draft_id,
        seen_revision=seen_revision_from_dto(body),
        proposed=proposed_profile_from_dto(body["payload"]),
    )
    return {"draft": draft_dto(view)}


@router.post("/api/drafts/{draft_id}/confirm")
async def confirm_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """所见 revision 确认草稿；返回持久化提交结果（重复确认返回原结果）。"""
    body = await json_object_body(request, keys=_CONFIRM_KEYS)
    result = await ConfirmService(request.app.state.db).confirm_profile_draft(
        draft_id=draft_id, seen_revision=seen_revision_from_dto(body)
    )
    return commit_result_dto(result)


@router.post("/api/drafts/{draft_id}/discard")
async def discard_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """丢弃待确认草稿：只改草稿状态，不撤销已提交事实（重复丢弃返回同一结果）。"""
    await json_object_body(request, keys=_DISCARD_KEYS)
    view = await DraftService(request.app.state.db).discard_draft(draft_id=draft_id)
    return {"draft_id": view.draft.id, "status": "discarded"}
