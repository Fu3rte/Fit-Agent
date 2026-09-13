"""Provider 配置路由：查询只返回 has_api_key（10.3）。

三条路径（拼写冻结见 ``frontend/plans/stage6-transport-freeze.md`` §1.4）：

- ``GET /api/provider``：安全投影 ``has_api_key`` 加只读配置展示（Provider 标识、协议、Base URL、
  模型 id 与部署位置）；**不含**完整 Key、不含掩码、不含数据目录。
- ``PUT /api/provider/api-key``：录入或替换（同库存储，10.3）；body 键恰为 ``api_key``，
  形状错误或空 Key 一律 400 ``invalid_request``。
- ``DELETE /api/provider/api-key``：显式删除；未配置也返回同一投影（幂等）。

密钥边界（10.3）：完整 Key 只作为绑定参数进入 ``SettingRepo``（写入期间压制驱动参数回显），
不出现在任何响应、日志、SSE 或异常详情；本层校验错误只描述形状，不回显 Key 内容。
协议名与 Base URL／模型 id 是只读配置展示（取自启动时冻结的模型目录），本路由不提供编辑面。
"""

from typing import Any

from fastapi import APIRouter, Request

from api.dto import InvalidRequestShape, json_object_body
from storage.errors import NotFound
from storage.setting_repo import SettingRepo

router = APIRouter()

#: 生产端点是 OpenAI 兼容接口（PLAN.md 技术栈「模型 Provider」；Stage 6 换商后仍为兼容端点）。
_PROTOCOL = "openai-compatible"
#: PUT 请求体字段：恰为 ``api_key``（不接受 provider 等其他字段）。
_ALLOWED_BODY_KEYS = frozenset({"api_key"})


def _provider_projection(request: Request, has_api_key: bool) -> dict[str, Any]:
    """公开查询投影：只暴露 has_api_key 与只读配置展示，绝不携带 Key 或掩码。

    provider 标识、Base URL 与模型 id 取启动时冻结的模型目录（单一来源），本路由不提供编辑面，
    也不改存储面。部署位置按目录事实给 ``cloud``（首版唯一受支持端点即云端兼容端点）。
    """
    spec = request.app.state.harness_config.spec
    return {
        "provider": spec.provider,
        "has_api_key": has_api_key,
        "protocol": _PROTOCOL,
        "base_url": spec.base_url,
        "model": {"name": spec.model_id, "deployment": "cloud"},
    }


@router.get("/api/provider")
async def get_provider(request: Request) -> dict[str, Any]:
    """Provider 查询：默认只返回 ``has_api_key``（10.3），无写入副作用。"""
    status = await SettingRepo(request.app.state.db).get_provider_status(
        request.app.state.harness_config.spec.provider
    )
    return _provider_projection(
        request, has_api_key=bool(status and status["has_api_key"])
    )


@router.put("/api/provider/api-key")
async def put_provider_api_key(request: Request) -> dict[str, Any]:
    """录入／替换 Key（10.3）：返回值只有 provider 标识与 ``has_api_key``。"""
    body = await json_object_body(request, keys=_ALLOWED_BODY_KEYS)
    api_key = body["api_key"]
    if not isinstance(api_key, str) or not api_key.strip():
        # 不回显传入值：错误详情里绝不出现 Key 内容（10.3）
        raise InvalidRequestShape("api_key 必须是非空字符串")
    await SettingRepo(request.app.state.db).set_provider_api_key(
        request.app.state.harness_config.spec.provider, api_key
    )
    # /healthz 读取的进程内投影同步刷新，避免查询面与健康检查互相打脸
    request.app.state.provider_has_api_key = True
    return {
        "provider": request.app.state.harness_config.spec.provider,
        "has_api_key": True,
    }


@router.delete("/api/provider/api-key")
async def delete_provider_api_key(request: Request) -> dict[str, Any]:
    """显式删除 Key（10.3）：清空后 ``has_api_key=false``；未配置同样返回 false（幂等）。"""
    try:
        await SettingRepo(request.app.state.db).delete_provider_api_key(
            request.app.state.harness_config.spec.provider
        )
    except NotFound:
        # 未配置即已达成目标：删除是幂等操作，不把「本来就没有」当未找到
        pass
    request.app.state.provider_has_api_key = False
    return {
        "provider": request.app.state.harness_config.spec.provider,
        "has_api_key": False,
    }
