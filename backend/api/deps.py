"""请求依赖注入：业务日期（固定业务时区下的自然日）。

本模块只保留**业务时间**注入（LANGGRAPH_REFACTOR_PLAN §5.5、Stage 1 子任务 04）：新版自然日
统一由 ``business_time.business_date()`` 按 lifespan 冻结的本机 IANA 时区折算，不接受客户端
传入（客户端时钟不是可信基线）。repo、领域服务与路由都不得自行调用 ``date.today()``。

测试用 FastAPI ``dependency_overrides`` 覆盖 :func:`business_today` 固定时钟，不靠系统真实日期。
"""

from datetime import UTC, date, datetime

from fastapi import Request

from business_time import business_date


def business_today(request: Request) -> datetime:
    """当刻绝对时刻（UTC）：真实时钟只在传输层读取一次，便于测试整体覆盖。"""
    return datetime.now(UTC)


def current_business_date(request: Request) -> date:
    """请求当刻的业务自然日：按本实例固定业务时区解释（REFACTOR_PLAN §5.5）。"""
    return business_date(
        business_today(request), str(request.app.state.business_timezone)
    )
