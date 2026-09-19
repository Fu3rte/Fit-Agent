# 模型配置路由：GET／PUT／DELETE／POST /api/provider（传输边界只经 provider_settings 读写 provider.json）。
# 响应契约：只回 has_api_key 布尔投影与 base_url／model 展示字段，完整 API Key 不进任何响应或日志；
# 错误复用 api/dto.py 的统一 JSON 形状 {http_status, error_code, message}，message 不含 Key、端点与堆栈。
# LoopbackGuard（api/app.py）对本路由同样生效；模型入口每次调用都重新 resolve，无版本号缓存。
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from api.dto import ProviderPutBody
from graph.model import probe_model_call
from provider_settings import (
    ProviderConfig,
    read_provider_config,
    write_provider_config,
)

router = APIRouter()


@router.get("/api/provider")
async def get_provider(request: Request) -> dict[str, Any]:
    """当前模型配置的展示投影：``has_api_key`` 布尔 ＋ 可见的 base_url／model，永不携带 Key 值。"""
    return _provider_dto(read_provider_config(_data_dir(request)))


@router.put("/api/provider")
async def put_provider(body: ProviderPutBody, request: Request) -> dict[str, Any]:
    """整份覆盖写入三字段：body 未出现的字段写空串，出现的字段按值写入；返回与 GET 同形。"""
    config = _body_config(body)
    write_provider_config(_data_dir(request), config)
    return _provider_dto(config)


@router.delete("/api/provider")
async def delete_provider(request: Request) -> dict[str, Any]:
    """清空三项（幂等）：重复删除同样返回 ``has_api_key: false`` 与全空展示字段。"""
    write_provider_config(_data_dir(request), ProviderConfig())
    return _provider_dto(ProviderConfig())


#: 连通性测试的固定文案：不含 Base URL、模型名、Key 与堆栈。
PROBE_OK_MESSAGE = "模型调用成功"
PROBE_FAILED_MESSAGE = "模型调用失败：请检查 Base URL、模型名与 API Key"


@router.post("/api/provider/test")
async def test_provider(body: ProviderPutBody, request: Request) -> dict[str, Any]:
    """用请求体覆盖（空字段沿用已存配置）发一次最小调用：不写 provider.json、不消耗 Run 预算。"""
    started = time.perf_counter()
    try:
        await probe_model_call(_data_dir(request), _body_config(body))
    except Exception:
        return {"ok": False, "latency_ms": None, "message": PROBE_FAILED_MESSAGE}
    return {
        "ok": True,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "message": PROBE_OK_MESSAGE,
    }


def _body_config(body: ProviderPutBody) -> ProviderConfig:
    return ProviderConfig(
        api_key=body.api_key or "",
        base_url=body.base_url or "",
        model=body.model or "",
    )


def _provider_dto(config: ProviderConfig) -> dict[str, Any]:
    return {
        "has_api_key": bool(config.api_key),
        "base_url": config.base_url,
        "model": config.model,
    }


def _data_dir(request: Request) -> Path:
    data_dir = getattr(request.app.state, "data_dir", None)
    if data_dir is None:
        raise RuntimeError("模型配置目录未装配：create_app 的 lifespan 尚未启动")
    return data_dir
