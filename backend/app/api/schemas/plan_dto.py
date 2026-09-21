"""计划与计划日程的响应 DTO。"""

from typing import Any

from app.domain.plans.schema import Plan, PlanSession


class UnknownResource(ValueError):
    """按身份读取的只读资源不存在（计划版本）。"""


def plan_dto(plan: Plan) -> dict[str, Any]:
    """一个计划版本 → 传输对象：行字段 + 结构化内容（已解码的合法 JSON）。"""
    return {
        "id": plan.id,
        "version": plan.version,
        "status": plan.status,
        "source_plan_id": plan.source_plan_id,
        "structured_content": plan.structured_content,
        "evaluator_result": plan.evaluator_result,
        "created_at": plan.created_at,
        "confirmed_at": plan.confirmed_at,
        "archived_at": plan.archived_at,
    }


def plan_session_dto(session: PlanSession) -> dict[str, Any]:
    """一条计划日程 → 传输对象；``cancelled_at`` 非空表示该日程已取消。"""
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "scheduled_on": session.scheduled_on.isoformat(),
        "cancelled_at": session.cancelled_at,
    }


def plan_session_candidate_dto(session: PlanSession) -> dict[str, Any]:
    """当天可关联的计划日程候选（未取消且未被其他训练关联）。"""
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "scheduled_on": session.scheduled_on.isoformat(),
    }
