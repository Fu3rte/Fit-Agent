"""请求依赖注入：业务日期（固定业务时区下的自然日）。"""

from datetime import UTC, date, datetime

from fastapi import Request

from business_time import business_date


def business_today(request: Request) -> datetime:
    """当刻绝对时刻（UTC）：真实时钟只在传输层读取一次，便于测试整体覆盖。"""
    return datetime.now(UTC)


def iso_now() -> str:
    """当刻绝对时刻的 ISO 文本：落库时间戳的唯一来源。"""
    return datetime.now(UTC).isoformat()


def current_business_date(request: Request) -> date:
    """请求当刻的业务自然日：按本实例固定业务时区解释（REFACTOR_PLAN §5.5）。"""
    return business_date(
        business_today(request), str(request.app.state.business_timezone)
    )
