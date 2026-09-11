"""草稿业务接口路由（01 1.2：草稿查询／纠错／确认／丢弃走业务接口，不靠历史通知）。

传输边界（stage2.md §5 S2-07；stage3.md §5 S3-14）：路由只校验传输形状、调用应用层、映射
结果与错误；领域规则（revision、业务基线、终态、首次建档完整性、计划／记录结构与引用）、
SQL 与事务编排都在 ``app/*.py`` 与各 repo。**草稿创建只在内部应用层**（不开放 HTTP 面），本
模块不提供创建草稿、重算或直接写正式事实的入口。

按草稿 ``kind`` 分派（不把一种载荷按另一种形状解码）：``profile_update`` 走 S2-05，
``plan`` 走 S3-05/S3-06，``training_record`` 走 S3-10/S3-11，``arrangement`` 走 S3-08。
分派只查草稿行的 kind（身份不存在即明确未找到），再调对应入口；各入口自身仍会再按 kind
拒绝不匹配的调用（双保险，不在本层静默改写形状）。
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.deps import current_business_date
from api.dto import (
    any_commit_result_dto,
    any_draft_dto,
    json_object_body,
    plan_revision_from_dto,
    proposed_profile_from_dto,
    record_payload_from_dto,
    seen_revision_from_dto,
)
from app.arrangement_drafts import ARRANGEMENT_DRAFT_KIND, ArrangementDraftService
from app.confirm import ConfirmService
from app.draft_repo import Draft, DraftRepo
from app.drafts import DraftService, UnknownDraft
from app.plan_drafts import PLAN_DRAFT_KIND, PlanDraftService
from app.record_drafts import RECORD_DRAFT_KIND, RecordDraftService

router = APIRouter()

_REVISE_KEYS = frozenset({"revision", "payload"})
_CONFIRM_KEYS = frozenset({"revision"})
_DISCARD_KEYS = frozenset()  # 丢弃请求体不接受任何字段


async def _draft_kind(db: Any, draft_id: str) -> str:
    """草稿行的 kind；身份不存在即明确未找到（不创建草稿、不猜测 kind）。"""
    draft = await DraftRepo(db).get(draft_id)
    if draft is None:
        raise UnknownDraft(f"草稿不存在：{draft_id}")
    return draft.kind


async def _view_dto(db: Any, draft_id: str) -> dict[str, Any]:
    """按 kind 分派到对应应用层查询入口，返回统一草稿传输对象。"""
    kind = await _draft_kind(db, draft_id)
    if kind == PLAN_DRAFT_KIND:
        view = await PlanDraftService(db).get_plan_draft(draft_id)
    elif kind == RECORD_DRAFT_KIND:
        view = await RecordDraftService(db).get_record_draft(draft_id)
    elif kind == ARRANGEMENT_DRAFT_KIND:
        view = await ArrangementDraftService(db).get_arrangement_draft(draft_id)
    else:
        view = await DraftService(db).get_draft(draft_id)
    if view is None:
        raise UnknownDraft(f"草稿不存在：{draft_id}")
    return any_draft_dto(view)


async def _view_for_row(db: Any, draft: Draft) -> dict[str, Any]:
    """已读到的草稿行 → 统一草稿传输对象（会话列表用，不重复做 kind 查询）。"""
    if draft.kind == PLAN_DRAFT_KIND:
        view = await PlanDraftService(db).get_plan_draft(draft.id)
    elif draft.kind == RECORD_DRAFT_KIND:
        view = await RecordDraftService(db).get_record_draft(draft.id)
    elif draft.kind == ARRANGEMENT_DRAFT_KIND:
        view = await ArrangementDraftService(db).get_arrangement_draft(draft.id)
    else:
        view = await DraftService(db).get_draft(draft.id)
    if view is None:
        raise UnknownDraft(f"草稿不存在：{draft.id}")
    return any_draft_dto(view)


@router.get("/api/sessions/{session_id}/drafts")
async def list_session_drafts(
    session_id: str, request: Request
) -> list[dict[str, Any]]:
    """该会话已持久化草稿的当前状态（含计划／记录／安排／档案 kind）；不依赖历史通知。"""
    db = request.app.state.db
    drafts = await DraftRepo(db).list_for_conversation(session_id)
    return [await _view_for_row(db, draft) for draft in drafts]


@router.get("/api/drafts/{draft_id}")
async def get_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """单草稿当前内容、revision、状态、结构化 Diff 与已提交结果；不存在即明确未找到。"""
    return await _view_dto(request.app.state.db, draft_id)


@router.post("/api/drafts/{draft_id}/revise")
async def revise_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """内容＋所见 revision 纠错 Pending 草稿；只改草稿，不自动提交。

    传输形状（非法 JSON／字段集）与 revision 形状先于身份查找校验；kind 决定载荷解码
    （档案三态事实／计划 D9 载荷＋日期／记录载荷），载荷形状不符即 400，不触碰应用层。
    """
    body = await json_object_body(request, keys=_REVISE_KEYS)
    seen_revision = seen_revision_from_dto(body)
    db = request.app.state.db
    kind = await _draft_kind(db, draft_id)
    if kind == PLAN_DRAFT_KIND:
        starts_on, review_on, payload = plan_revision_from_dto(body)
        view = await PlanDraftService(db).revise_plan_draft(
            draft_id=draft_id,
            seen_revision=seen_revision,
            payload=payload,
            starts_on=starts_on,
            review_on=review_on,
        )
    elif kind == RECORD_DRAFT_KIND:
        record_payload = record_payload_from_dto(body)
        view = await RecordDraftService(db).revise_record_draft(
            draft_id=draft_id,
            seen_revision=seen_revision,
            payload=record_payload,
        )
    else:
        view = await DraftService(db).revise_profile_draft(
            draft_id=draft_id,
            seen_revision=seen_revision,
            proposed=proposed_profile_from_dto(body["payload"]),
        )
    return {"draft": any_draft_dto(view)}


@router.post("/api/drafts/{draft_id}/confirm")
async def confirm_draft(
    draft_id: str,
    request: Request,
    business_date: date = Depends(current_business_date),
) -> dict[str, Any]:
    """所见 revision 确认草稿；返回持久化提交结果（重复确认返回原结果）。

    计划确认需要当刻**业务日期**（到期即锁的规则判定，07 7.3）：由服务端按固定业务时区算出
    并注入，不接受客户端传入。
    """
    body = await json_object_body(request, keys=_CONFIRM_KEYS)
    seen_revision = seen_revision_from_dto(body)
    db = request.app.state.db
    kind = await _draft_kind(db, draft_id)
    confirm = ConfirmService(db)
    if kind == PLAN_DRAFT_KIND:
        result = await confirm.confirm_plan_draft(
            draft_id=draft_id,
            seen_revision=seen_revision,
            business_date=business_date,
        )
    elif kind == RECORD_DRAFT_KIND:
        result = await confirm.confirm_record_draft(
            draft_id=draft_id, seen_revision=seen_revision
        )
    elif kind == ARRANGEMENT_DRAFT_KIND:
        result = await confirm.confirm_arrangement_draft(
            draft_id=draft_id, seen_revision=seen_revision
        )
    else:
        result = await confirm.confirm_profile_draft(
            draft_id=draft_id, seen_revision=seen_revision
        )
    return any_commit_result_dto(result)


@router.post("/api/drafts/{draft_id}/void")
async def void_record_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """作废整次训练：向既有身份追加 ``voided`` 修订（不物理删除、不回退）；仅记录草稿。"""
    body = await json_object_body(request, keys=_CONFIRM_KEYS)
    seen_revision = seen_revision_from_dto(body)
    result = await ConfirmService(request.app.state.db).void_record_draft(
        draft_id=draft_id, seen_revision=seen_revision
    )
    return any_commit_result_dto(result)


@router.post("/api/drafts/{draft_id}/discard")
async def discard_draft(draft_id: str, request: Request) -> dict[str, Any]:
    """丢弃待确认草稿：只改草稿状态，不撤销已提交事实（重复丢弃返回同一结果）。

    计划与记录草稿走各自丢弃入口（只改状态、终态不可撤销）；安排草稿目前没有丢弃入口，
    经档案入口的 kind 分派明确拒绝，不静默按档案形状处理。
    """
    await json_object_body(request, keys=_DISCARD_KEYS)
    db = request.app.state.db
    kind = await _draft_kind(db, draft_id)
    if kind == PLAN_DRAFT_KIND:
        view = await PlanDraftService(db).discard_plan_draft(draft_id=draft_id)
    elif kind == RECORD_DRAFT_KIND:
        view = await RecordDraftService(db).discard_record_draft(draft_id=draft_id)
    else:
        view = await DraftService(db).discard_draft(draft_id=draft_id)
    return {"draft_id": view.draft.id, "status": view.draft.status}
