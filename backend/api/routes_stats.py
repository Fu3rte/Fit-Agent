"""统计只读路由：三类 PB、趋势与月历，无任何写入入口。"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.deps import current_business_date
from api.dto import (
    MonthQuery,
    calendar_dto,
    personal_best_dto,
    trends_dto,
)
from domain.stats.service import StatsService

router = APIRouter()


@router.get("/api/stats/personal-bests")
async def list_personal_bests(request: Request) -> dict[str, Any]:
    """三类 PB（最大重量、最大次数、最长时长）及来源训练、组序号与日期；现算，不落表。"""
    bests = await StatsService(request.app.state.db).list_personal_bests()
    return {"personal_bests": [personal_best_dto(pb) for pb in bests]}


@router.get("/api/stats/trends")
async def get_trends(
    request: Request, business_day: date = Depends(current_business_date)
) -> dict[str, Any]:
    """最近 30 天体重／体脂原始点、力量累计 PB 系列与确定性趋势摘要。"""
    report = await StatsService(request.app.state.db).trends(business_day)
    return {"trends": trends_dto(report)}


@router.get("/api/stats/calendar")
async def get_calendar(request: Request, month: MonthQuery) -> dict[str, Any]:
    """一个自然月的计划日程状态与实际训练事实；``month`` 为严格的 ``YYYY-MM``。"""
    year, month_number = int(month[:4]), int(month[5:])
    calendar = await StatsService(request.app.state.db).calendar_month(year, month_number)
    return {"calendar": calendar_dto(calendar)}
