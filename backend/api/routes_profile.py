"""画像表单路由：读取与整份更新（Stage 1 子任务 04 §9）。

传输边界：只调用 ``domain.profile.service.ProfileService``（读／整份覆盖写），不做领域规则、
不写 SQL；请求体形状与错误映射见 :mod:`api.dto`。未建档时发 ``profile: null``，不伪造空事实。
"""

from typing import Any

from fastapi import APIRouter, Request

from api.dto import ProfileBody, profile_facts_dto, profile_from_dto
from domain.profile.service import ProfileService

router = APIRouter()


@router.get("/api/profile")
async def get_profile(request: Request) -> dict[str, Any]:
    """当前画像；未建档（``profile_json`` 为 NULL）时发 ``profile: null``。"""
    profile = await ProfileService(request.app.state.db).read()
    return {"profile": None if profile is None else profile_facts_dto(profile)}


@router.put("/api/profile")
async def put_profile(body: ProfileBody, request: Request) -> dict[str, Any]:
    """整份覆盖写入画像七字段（PUT 语义）：未填写用 ``unknown``，明确为空用 ``denied``。

    空列表的 ``known`` 由领域校验拒绝（422），本层不改写成 ``denied``。
    """
    profile = profile_from_dto(body)
    await ProfileService(request.app.state.db).update(profile)
    return {"profile": profile_facts_dto(profile)}
