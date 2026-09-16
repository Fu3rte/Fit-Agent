"""Fit-Agent 本地 FastAPI 应用基线。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from api import dto, routes_plans, routes_profile, routes_records, routes_stats
from config import (
    database_path,
    frontend_dist_dir,
    local_timezone_name,
    model_api_key_configured,
    resolve_data_dir,
)
from storage.db import Database

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


def _is_loopback_authority(authority: str) -> bool:
    hostname = urlsplit(f"//{authority}").hostname
    return hostname is not None and hostname.lower() in _LOOPBACK_HOSTNAMES


async def _send_forbidden(send) -> None:
    body = b'{"detail":"forbidden"}'
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class LoopbackGuardMiddleware:
    """只允许回环 Host，存在 Origin 时也只允许回环来源。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            host = _header_value(headers, b"host")
            if host is None or not _is_loopback_authority(host):
                await _send_forbidden(send)
                return
            origin = _header_value(headers, b"origin")
            if origin is not None:
                origin_host = urlsplit(origin).hostname
                if origin_host is None or origin_host.lower() not in _LOOPBACK_HOSTNAMES:
                    await _send_forbidden(send)
                    return
        await self.app(scope, receive, send)


def create_app(
    data_dir: str | Path | None = None, *, frontend_dist: str | Path | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = resolve_data_dir(data_dir)
        resolved.mkdir(parents=True, exist_ok=True)
        db = Database(database_path(resolved))
        app.state.db = db
        try:
            await db.open()
            # 启动即执行新版 001_initial.sql 建立业务表；迁移失败则启动失败（不对外开放）。
            await db.migrate()
            app.state.business_timezone = local_timezone_name()
            app.state.provider_has_api_key = model_api_key_configured()
            yield
        finally:
            await db.close()

    app = FastAPI(
        title="Fit-Agent",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(LoopbackGuardMiddleware)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, object]:
        db: Database = request.app.state.db
        return {
            "status": "ok",
            "database": "open" if db.is_open else "closed",
            "business_timezone": request.app.state.business_timezone,
            "provider_has_api_key": request.app.state.provider_has_api_key,
        }

    # 表单 API 路由必须先于 /{path:path} 静态兜底注册，否则会被前端宿主吞掉。
    dto.install_error_handlers(app)
    app.include_router(routes_profile.router)
    app.include_router(routes_records.router)
    app.include_router(routes_plans.router)
    app.include_router(routes_stats.router)
    _install_frontend_static(
        app,
        Path(frontend_dist) if frontend_dist is not None else frontend_dist_dir(),
    )
    return app


def _install_frontend_static(app: FastAPI, dist_dir: Path) -> None:
    index = dist_dir / "index.html"

    @app.get("/{path:path}")
    async def serve_frontend(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        if path and (candidate := _dist_file(dist_dir, path)) is not None:
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404)


def _dist_file(dist_dir: Path, path: str) -> Path | None:
    candidate = (dist_dir / path).resolve()
    if candidate.is_relative_to(dist_dir.resolve()) and candidate.is_file():
        return candidate
    return None
