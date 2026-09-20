import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from api.dto import ProviderPutBody
from graph.model import probe_model_call, probe_structured_model_call
from provider_settings import (
    DEFAULT_API,
    DEFAULT_STRUCTURED_OUTPUT,
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
    """写入五字段：base_url／model／api／structured_output 以 body 为准（协议字段空串回落默认），api_key 空串沿用已存值；返回与 GET 同形。"""
    data_dir = _data_dir(request)
    override = _body_override(body)
    config = replace(
        override,
        api_key=override.api_key or read_provider_config(data_dir).api_key,
        api=override.api or DEFAULT_API,
        structured_output=override.structured_output or DEFAULT_STRUCTURED_OUTPUT,
    )
    write_provider_config(data_dir, config)
    return _provider_dto(config)


@router.delete("/api/provider")
async def delete_provider(request: Request) -> dict[str, Any]:
    """清空凭据（幂等）：重复删除同样返回 ``has_api_key: false``、协议默认与全空展示字段。"""
    write_provider_config(_data_dir(request), ProviderConfig())
    return _provider_dto(ProviderConfig())


PROBE_OK_MESSAGE = "模型调用成功"
PROBE_FAILED_MESSAGE = "模型调用失败：请检查 Base URL、模型名与 API Key"
PROBE_STRUCTURED_FAILED_MESSAGE = "模型调用成功，但未通过所选结构化输出能力探针"


@router.post("/api/provider/test")
async def test_provider(body: ProviderPutBody, request: Request) -> dict[str, Any]:
    """用请求体覆盖（空字段沿用已存配置）发两次最小调用：不写 provider.json、不消耗 Run 预算。"""
    data_dir = _data_dir(request)
    config = _body_override(body)
    started = time.perf_counter()
    try:
        await probe_model_call(data_dir, config)
    except Exception:
        return {"ok": False, "latency_ms": None, "message": PROBE_FAILED_MESSAGE}
    try:
        await probe_structured_model_call(data_dir, config)
    except Exception:
        return {
            "ok": False,
            "latency_ms": None,
            "message": PROBE_STRUCTURED_FAILED_MESSAGE,
        }
    return {
        "ok": True,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "message": PROBE_OK_MESSAGE,
    }


def _body_override(body: ProviderPutBody) -> ProviderConfig:
    """请求体 → 覆盖配置：空字段保持空串，覆盖还是沿用交给调用方决定。"""
    return ProviderConfig(
        api_key=body.api_key or "",
        base_url=body.base_url or "",
        model=body.model or "",
        api=(body.api or "").strip(),
        structured_output=(body.structured_output or "").strip(),
    )


def _provider_dto(config: ProviderConfig) -> dict[str, Any]:
    return {
        "has_api_key": bool(config.api_key),
        "base_url": config.base_url,
        "model": config.model,
        "api": config.api,
        "structured_output": config.structured_output,
    }


def _data_dir(request: Request) -> Path:
    data_dir = getattr(request.app.state, "data_dir", None)
    if data_dir is None:
        raise RuntimeError("模型配置目录未装配：create_app 的 lifespan 尚未启动")
    return data_dir
