"""只读看板路由：正式档案（S2-07）。计划／记录／统计仍未接线（Stage 3 起）。

传输边界（stage2.md §5 S2-07）：只调用应用层读取入口并映射响应形状，不做领域规则、
不写库、不持独立 SQL；响应形状与错误映射见 :mod:`api.dto`。
"""

from typing import Any

from fastapi import APIRouter, Request

from api.dto import profile_response_dto
from domain.profile.service import ProfileService

router = APIRouter()


@router.get("/api/profile")
async def get_profile(request: Request) -> dict[str, Any]:
    """当前正式档案与统一业务版本；未建档发 ``profile: null``，无写入副作用。"""
    snapshot = await ProfileService(request.app.state.db).read_formal_profile()
    return profile_response_dto(snapshot)
