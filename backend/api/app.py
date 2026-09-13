"""FastAPI 工厂 + lifespan：连接/迁移/回环监听/Host-Origin 校验/业务路由装配。

Stage 0 实现范围：生命周期内打开唯一连接并完成迁移、读取固定配置、
Host/Origin 校验中间件与健康检查；业务路由（routes_*.py）与前端静态托管
归后续阶段。启动失败（目录不可建/连接失败/迁移失败）不继续对外提供服务，
并关闭本应用已建立的连接（S0-02）。

Stage 2 S2-07 装配：只读档案路由与草稿业务路由（纠错/确认/丢弃）在已有路由位置
接线，错误形状统一由 ``api.dto.install_error_handlers`` 注册。

Stage 4 S4-07 装配：对话／Run 路由与 SSE（``api.routes_chat``）、进程内事件流
（``runtime.events``）、唯一执行驱动（``runtime.run_task``，带状态观察者）与处理每 Run 一次的
生产模型工厂（``api.deps.make_model_factory``）在 lifespan 内装配；模型仍只在本机调用
Provider，本层不打印、不落盘凭据。

Stage 6 F6-01 装配：Provider 设置路由（``api.routes_settings``，10.3）与前端构建产物静态托管＋
SPA 回退（10.1，``frontend/dist``，运行时不需要 Node.js）；静态路由最后注册，不抢 ``/api/*`` 与
``/healthz``。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from api.deps import make_model_factory
from api.dto import install_error_handlers
from api.routes_chat import router as chat_router
from api.routes_drafts import router as drafts_router
from api.routes_readonly import router as readonly_router
from api.routes_settings import router as settings_router
from app.draft_repo import DraftRepo
from app.drafts import DraftService
from config import (
    database_path,
    freeze_effective_harness,
    frontend_dist_dir,
    local_timezone_name,
    resolve_data_dir,
)
from runtime.events import RunEventStream
from runtime.run_service import RunService
from runtime.run_task import ExecutionDriver
from storage.db import Database
from storage.run_repo import RunRepo
from storage.setting_repo import SettingRepo

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


def create_app(
    data_dir: str | Path | None = None, *, frontend_dist: str | Path | None = None
) -> FastAPI:
    """装配最小后端应用。

    ``data_dir`` 仅用于测试与人工验收的临时数据隔离；``frontend_dist`` 仅用于测试与打包裁剪
    时显式指定前端构建产物目录（缺省即 10.1 固定的 ``frontend/dist``）。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = resolve_data_dir(data_dir)
        resolved.mkdir(parents=True, exist_ok=True)
        # 08 8.5（S4-05a）：Harness 本地配置在启动时加载并做硬边界与容量交叉校验；越界、
        # 未知模型或不变量不成立一律拒绝启动，不静默钳制。Run 开始时的冻结入口同为此函数
        # （S4-05b/07 每次 Run 开始时冻结；运行中改文件不影响已冻结的配置）。
        app.state.harness_config = freeze_effective_harness(resolved)
        db = Database(database_path(resolved))
        app.state.db = db
        try:
            await db.open()
            await db.migrate()
            # 08 8.4：迁移完成后，在一个事务内把遗留 pending/running 标为 failed 并追加
            # interrupted_by_restart 事件；不做断点续跑，由用户手动重试（S4-02）。
            # 恢复先于对外服务：启动失败则拒绝启动，不带着“还在跑”的假状态服务请求。
            repo = RunRepo(db)
            await RunService(repo).recover_interrupted_runs()
            settings = SettingRepo(db)
            # 固定业务时区（07 7.3 / S0-05）：首次启动采样本机时区并持久化，
            # 之后只读已保存值，不随系统时区变化重取。采样或解析失败向上抛，
            # 由启动失败拒绝对外服务，不静默降级为 UTC 或固定偏移。
            app.state.business_timezone = (
                await settings.initialize_business_timezone(local_timezone_name)
            )["timezone"]
            provider = await settings.get_provider_status(
                app.state.harness_config.spec.provider
            )
            app.state.provider_has_api_key = bool(provider and provider["has_api_key"])
            # S4-07：进程内事件流（只服务实时显示，不落库、不重放）与**唯一**执行驱动；
            # 驱动的状态观察者只把状态变化转成产品事件，不参与状态判定（08 8.2/8.7）。
            events = RunEventStream()
            driver = ExecutionDriver(repo, on_status=events.publish_status)
            app.state.run_events = events
            app.state.run_driver = driver
            app.state.run_service = RunService(
                repo,
                active_execution=lambda: driver.active_run_id,
                drafts=DraftRepo(db),
                baseline=DraftService(db),
            )
            # 每次 Run 开始时才读一次凭据并构造模型（凭据只在进程内流转，10.3）；
            # 本 Run 的限制取启动时冻结的有效 Harness（08 8.5）。
            app.state.model_factory = make_model_factory(db, app.state.harness_config)
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
    app.include_router(chat_router)
    app.include_router(settings_router)
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

    _install_frontend_static(
        app,
        Path(frontend_dist) if frontend_dist is not None else frontend_dist_dir(),
    )
    return app


def _install_frontend_static(app: FastAPI, dist_dir: Path) -> None:
    """托管前端生产构建产物并做 SPA 回退（10.1）。

    本路由**最后**注册，因此已注册的 ``/api/*`` 与 ``/healthz`` 先匹配、不被静态托管抢走：
    仅当路径未命中业务路由时才进这里。命中 ``dist`` 内真实文件即按文件返回（含
    ``index.html`` 与 ``assets/*``）；其余非 ``/api`` 路径回退 ``index.html``（前端路由由浏览器侧
    解释）。``/api`` 前缀下的未匹配路径仍是 404，不把 API 404 变成 HTML；``dist`` 缺失
    （未构建／未打包）时非 ``/api`` 路径同样 404，不假装前端已交付。
    """
    index = dist_dir / "index.html"

    @app.get("/{path:path}")
    async def serve_frontend(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        if path:
            candidate = _dist_file(dist_dir, path)
            if candidate is not None:
                return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404)


def _dist_file(dist_dir: Path, path: str) -> Path | None:
    """``dist`` 内的真实文件路径；越界（``..`` 逃逸）与不存在的路径一律 ``None``。"""
    candidate = (dist_dir / path).resolve()
    if candidate.is_relative_to(dist_dir.resolve()) and candidate.is_file():
        return candidate
    return None
