"""画像表单路由：读取与整份更新。"""

from typing import Any

from fastapi import APIRouter, Request

from app.api.dependencies import app_services
from app.api.schemas.profile_dto import (
    ProfileBody,
    profile_facts_dto,
    profile_from_dto,
)

router = APIRouter()


@router.get("/api/profile")
async def get_profile(request: Request) -> dict[str, Any]:
    """当前画像；未建档（``profile_json`` 为 NULL）时发 ``profile: null``。"""
    profile = await app_services(request).profile.read()
    return {"profile": None if profile is None else profile_facts_dto(profile)}


@router.put("/api/profile")
async def put_profile(body: ProfileBody, request: Request) -> dict[str, Any]:
    """整份覆盖写入画像七字段（PUT 语义）：未填写用 ``unknown``，明确为空用 ``denied``。"""
    profile = profile_from_dto(body)
    await app_services(request).profile.update(profile)
    return {"profile": profile_facts_dto(profile)}
