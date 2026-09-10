"""FastAPI 工厂 + lifespan：连接/迁移/回环监听/Host-Origin 校验/业务路由装配。

Stage 0 实现范围：生命周期内打开唯一连接并完成迁移、读取固定配置、
Host/Origin 校验中间件与健康检查；业务路由（routes_*.py）与前端静态托管
归后续阶段。启动失败（目录不可建/连接失败/迁移失败）不继续对外提供服务，
并关闭本应用已建立的连接（S0-02）。

Stage 2 S2-07 装配：只读档案路由与草稿业务路由（纠错/确认/丢弃）在已有路由位置
接线，错误形状统一由 ``api.dto.install_error_handlers`` 注册；创建草稿、重算、
聊天/Run 与设置面仍不在此阶段（stage2.md §5 S2-07）。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request

from api.dto import install_error_handlers
from api.routes_drafts import router as drafts_router
from api.routes_readonly import router as readonly_router
from config import database_path, local_timezone_name, resolve_data_dir
from storage.db import Database
from storage.setting_repo import DEFAULT_PROVIDER, SettingRepo

# 10.1：仅本机回环访问；Host/Origin 校验只放行回环主机名
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
    """纯 ASGI 中间件：基本 Host/Origin 校验（10.1，不因发布工作延期而省略）。

    Host 缺失或非回环 → 403；带 Origin 头时其主机非回环 → 403。
    """

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
                if (
                    origin_host is None
                    or origin_host.lower() not in _LOOPBACK_HOSTNAMES
                ):
                    await _send_forbidden(send)
                    return
        await self.app(scope, receive, send)


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    """装配最小后端应用；``data_dir`` 仅用于测试与人工验收的临时数据隔离。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = resolve_data_dir(data_dir)
        resolved.mkdir(parents=True, exist_ok=True)
        db = Database(database_path(resolved))
        app.state.db = db
        try:
            await db.open()
            await db.migrate()
            settings = SettingRepo(db)
            # 固定业务时区（07 7.3 / S0-05）：首次启动采样本机时区并持久化，
            # 之后只读已保存值，不随系统时区变化重取。采样或解析失败向上抛，
            # 由启动失败拒绝对外服务，不静默降级为 UTC 或固定偏移。
            app.state.business_timezone = (
                await settings.initialize_business_timezone(local_timezone_name)
            )["timezone"]
            provider = await settings.get_provider_status(DEFAULT_PROVIDER)
            app.state.provider_has_api_key = bool(provider and provider["has_api_key"])
            yield
        finally:
            # 唯一退出路径都关闭本应用连接（S0-02）：启动失败、正常停服，
            # 以及运行阶段异常或取消退出。close 本身取消安全：取消退出时
            # 也会等底层关闭 settle 后再传播取消，不遗留连接。
            # （open 失败时 db 未打开，close 是安全空操作。）
            await db.close()

    app = FastAPI(
        title="Fit-Agent",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(LoopbackGuardMiddleware)
    app.include_router(readonly_router)
    app.include_router(drafts_router)
    install_error_handlers(app)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, object]:
        """非业务健康检查：仅暴露服务状态，不读取业务数据与密钥。"""
        db: Database = request.app.state.db
        return {
            "status": "ok",
            "database": "open" if db.is_open else "closed",
            "business_timezone": request.app.state.business_timezone,
            "provider_has_api_key": request.app.state.provider_has_api_key,
        }

    return app
