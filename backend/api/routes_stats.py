"""统计只读路由（stage2.md §9、Subtask 04 §C）：三类 PB、趋势与月历，无任何写入入口。

传输边界：只调用 ``domain.stats.service.StatsService``（只读用例），不做统计规则、不写 SQL、
不重算前端需要的任何数值；字段与对象包裹形状由 :mod:`api.dto` 唯一给出。

两条硬约束：

- **注册顺序**：本 router 必须在静态前端兜底 ``/{path:path}`` 之前注册（``api/app.py``），
  否则 ``/api/stats/*`` 会被前端宿主吞掉。
- **业务日期注入**：趋势窗口与停训天数的「今天」由 :func:`api.deps.current_business_date` 按固定
  业务时区注入，不接受客户端传入；月历只按请求的 ``YYYY-MM`` 读取事实，与「今天」无关。
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.deps import current_business_date
from api.dto import (
    MonthQuery,
    calendar_dto,
    decode_year_month,
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
    year, month_number = decode_year_month(month)
    calendar = await StatsService(request.app.state.db).calendar_month(year, month_number)
    return {"calendar": calendar_dto(calendar)}
