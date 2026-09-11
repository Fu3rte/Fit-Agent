"""请求依赖注入：连接、锁、服务句柄。

S3-14 增加**业务日期**依赖：按固定业务时区（07 7.3）把当刻绝对时刻折算成业务自然日。业务
日期由服务端算出（计划确认、到期锁定、指导复核都要求「当刻」按业务时区解释），不接受客户端
传入——客户端时钟不是可信基线。测试用 FastAPI ``dependency_overrides`` 覆盖 `business_today`
固定时钟，不靠系统真实日期（stage3.md §6「固定时钟」）。
"""

from datetime import UTC, date, datetime

from fastapi import Request

from storage.setting_repo import business_date


def business_today(request: Request) -> datetime:
    """当刻绝对时刻（UTC）：真实时钟只在传输层读取一次，便于测试整体覆盖。"""
    return datetime.now(UTC)


def current_business_date(request: Request) -> date:
    """请求当刻的业务自然日：按本实例固定业务时区解释（07 7.3）。"""
    return business_date(
        business_today(request), str(request.app.state.business_timezone)
    )
