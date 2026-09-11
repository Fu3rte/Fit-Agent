"""只读看板路由：正式档案（S2-07）、计划／日程（S3-14）、记录、统计、复盘查询。

传输边界（stage2.md §5 S2-07；stage3.md §5 S3-14）：只调用应用层读取入口并映射响应形状，
不做领域规则、不写库、不持独立 SQL；响应形状与错误映射见 :mod:`api.dto`。

计划与日程读取**复用** :class:`~app.plan_reads.PlanReadService`（应用层投影，不在本层重建
第二份计划／日程投影）：当前计划、历史版本、以及「基于计划的指导」前置整份计划安全复核。
业务日期由 :func:`api.deps.current_business_date` 按固定业务时区注入（07 7.3），不接受客户端
传入；无正式计划/记录/统计结果时给 ``null``，不伪造空事实。
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.deps import current_business_date
from api.dto import (
    InvalidRequestShape,
    UnknownResource,
    guidance_dto,
    plan_view_dto,
    profile_response_dto,
    record_dto,
    review_dto,
    target_judgement_dto,
    week_completion_dto,
)
from app.plan_reads import PlanReadService
from app.review_store import ReviewStore
from domain.profile.service import ProfileService
from domain.records.service import RecordReadService
from domain.stats.service import StatsService

router = APIRouter()


@router.get("/api/profile")
async def get_profile(request: Request) -> dict[str, Any]:
    """当前正式档案与统一业务版本；未建档发 ``profile: null``，无写入副作用。"""
    snapshot = await ProfileService(request.app.state.db).read_formal_profile()
    return profile_response_dto(snapshot)


@router.get("/api/plan")
async def get_current_plan(
    request: Request, business_date: date = Depends(current_business_date)
) -> dict[str, Any]:
    """当前正式计划与全部日程（含已取消／已锁定）；尚无正式计划发 ``plan: null``。"""
    view = await PlanReadService(request.app.state.db).read_current_plan(
        business_date=business_date
    )
    return {"plan": None if view is None else plan_view_dto(view)}


@router.get("/api/plans/{plan_version_id}")
async def get_plan_version(
    plan_version_id: str,
    request: Request,
    business_date: date = Depends(current_business_date),
) -> dict[str, Any]:
    """按身份读取计划版本（含历史版本，历史不重激活）；不存在即明确未找到。"""
    view = await PlanReadService(request.app.state.db).read_plan_version(
        plan_version_id, business_date=business_date
    )
    if view is None:
        raise UnknownResource(f"计划版本不存在：{plan_version_id}")
    return {"plan": plan_view_dto(view)}


@router.get("/api/plan/guidance")
async def get_plan_guidance(
    request: Request,
    business_date: date = Depends(current_business_date),
    arrangement_revision_id: str | None = None,
) -> dict[str, Any]:
    """基于计划的指导前置复核：整份计划按最新限制与红旗复核，阻断时不给可执行建议。

    带 ``arrangement_revision_id`` 时按该已接受安排（当次条件）复核它**绑定的计划版本**与当次
    目标（04 4.3：未来安排使用时仍须按最新限制与红旗复核）；无正式计划发 ``guidance: null``。
    """
    service = PlanReadService(request.app.state.db)
    if arrangement_revision_id is not None:
        guidance = await service.read_arrangement_guidance(
            arrangement_revision_id, business_date=business_date
        )
        if guidance is None:
            raise UnknownResource(f"安排修订不存在：{arrangement_revision_id}")
    else:
        guidance = await service.read_current_plan_guidance(business_date=business_date)
    if guidance is None:
        return {"guidance": None}
    return {"guidance": guidance_dto(guidance, reviewed_at=_reviewed_at())}


@router.get("/api/records")
async def list_records(request: Request) -> dict[str, Any]:
    """全部训练身份与当前修订事实（同日多练各自身份；旧修订不重复出现）。"""
    views = await RecordReadService(request.app.state.db).list_records()
    return {"records": [record_dto(view) for view in views]}


@router.get("/api/records/{session_id}")
async def get_record(session_id: str, request: Request) -> dict[str, Any]:
    """按身份读取一次训练与当前修订；不存在即明确未找到（不按日期推测归属）。"""
    view = await RecordReadService(request.app.state.db).read_record(session_id)
    if view is None:
        raise UnknownResource(f"训练身份不存在：{session_id}")
    return {"record": record_dto(view)}


@router.get("/api/records/{session_id}/judgement")
async def get_record_judgement(session_id: str, request: Request) -> dict[str, Any]:
    """该次训练当前修订的组级三桶判定；无对照／已作废／无当前修订发 ``judgement: null``。"""
    db = request.app.state.db
    session = await RecordReadService(db).read_record(session_id)
    if session is None:
        raise UnknownResource(f"训练身份不存在：{session_id}")
    judgement = await StatsService(db).judge_session(session_id)
    return {"judgement": None if judgement is None else target_judgement_dto(judgement)}


@router.get("/api/stats/completion")
async def get_week_completion(
    request: Request,
    plan_version_id: str | None = None,
    week_no: str | None = None,
    business_date: date = Depends(current_business_date),
) -> dict[str, Any]:
    """同一计划版本同一 Wn 的完成率；没有已到期应训练次数发 ``completion: null``（「暂无」）。

    必填查询参数缺省由 :func:`_required_query` 给统一 400（不借用 FastAPI 默认缺参 422
    ``{"detail": [...]}``，S2-07 错误形状）。
    """
    version_id = _required_query("plan_version_id", plan_version_id)
    parsed_week = _positive_int("week_no", _required_query("week_no", week_no))
    completion = await StatsService(request.app.state.db).weekly_completion(
        version_id, parsed_week, business_date=business_date
    )
    return {
        "completion": None if completion is None else week_completion_dto(completion)
    }


@router.get("/api/stats/pr")
async def get_pr(
    request: Request,
    exercise_id: str | None = None,
    load_notation: str | None = None,
    load_kg_key: str | None = None,
) -> dict[str, Any]:
    """现算 PR：给 ``load_kg_key`` 时是该重量下的单组最高次数；否则是该口径最高重量。

    无候选即 ``null``（不是 0）；同重量不累计多组（06 6.3）。必填查询参数缺省给统一 400
    （同 :func:`get_week_completion`）。
    """
    exercise = _required_query("exercise_id", exercise_id)
    notation = _required_query("load_notation", load_notation)
    stats = StatsService(request.app.state.db)
    if load_kg_key is None:
        return {
            "pr": {
                "exercise_id": exercise,
                "load_notation": notation,
                "load_kg_key": None,
                "max_load_kg_key": await stats.pr_max_load(
                    exercise_id=exercise, load_notation=notation
                ),
                "best_reps": None,
            }
        }
    key = _positive_int("load_kg_key", load_kg_key)
    return {
        "pr": {
            "exercise_id": exercise,
            "load_notation": notation,
            "load_kg_key": key,
            "max_load_kg_key": None,
            "best_reps": await stats.pr_max_reps_at_load(
                exercise_id=exercise, load_notation=notation, load_kg_key=key
            ),
        }
    }


@router.get("/api/reviews")
async def list_reviews(request: Request) -> dict[str, Any]:
    """按生成顺序列出全部复盘（追加语义：重生成在后，旧复盘仍可读）。"""
    views = await ReviewStore(request.app.state.db).list_reviews()
    return {"reviews": [review_dto(view) for view in views]}


@router.get("/api/reviews/{review_id}")
async def get_review(review_id: str, request: Request) -> dict[str, Any]:
    """按身份读取一条复盘（正文与生成时快照原样返回，``stale`` 现算）；不存在即未找到。"""
    view = await ReviewStore(request.app.state.db).read_review(review_id)
    if view is None:
        raise UnknownResource(f"复盘不存在：{review_id}")
    return {"review": review_dto(view)}


def _required_query(name: str, raw: str | None) -> str:
    """必填查询参数缺省给统一 400 形状（不借用 FastAPI 默认缺参 422 ``{"detail": [...]}``）。"""
    if raw is None:
        raise InvalidRequestShape(f"{name} 为必填查询参数")
    return raw


def _positive_int(name: str, raw: str) -> int:
    """查询参数按文本收，手工解析：类型不符给统一 400 形状（不借用 FastAPI 的 422 形状）。"""
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestShape(f"{name} 必须是整数：{raw!r}") from exc
    if value < 1:
        raise InvalidRequestShape(f"{name} 必须是 >= 1 的整数：{raw!r}")
    return value


def _reviewed_at() -> str:
    """指导复核的传输层时刻（仅展示；安全判定本身与它无关，不落库）。"""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
