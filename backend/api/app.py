"""Fit-Agent 本地 FastAPI 应用基线。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from langgraph.checkpoint.base import BaseCheckpointSaver

from api import (
    dto,
    routes_agent,
    routes_plans,
    routes_profile,
    routes_provider,
    routes_records,
    routes_stats,
)
from config import (
    checkpoint_database_path,
    database_path,
    frontend_dist_dir,
    local_timezone_name,
    resolve_data_dir,
)
from domain.actions.service import ActionCatalogService
from domain.plans.service import (
    PlanActivationService,
    PlanPersistenceService,
    PlanReadService,
)
from domain.profile.service import ProfileService
from domain.records.service import WorkoutRecordsService
from domain.stats.repo import StatsRepo
from domain.stats.service import StatsService
from graph.checkpointer import open_checkpointer
from graph.context import MemoryAssembler
from graph.model import (
    MODEL_CALL_FAILED_MESSAGE,
    ModelCall,
    ModelCallFailed,
    ModelConfigurationError,
    openai_compatible_model_call,
)
from graph.nodes import GeneratePlanDeps
from graph.skills import SkillLoader
from graph.workflow import AgentRunDeps, build_generate_plan_graph
from provider_settings import provider_api_key_configured
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
        app.state.data_dir = resolved
        db = Database(database_path(resolved))
        app.state.db = db
        try:
            await db.open()
            # 启动即执行新版 001_initial.sql 建立业务表；迁移失败则启动失败（不对外开放）。
            await db.migrate()
            # Checkpoint 存档独立于业务库：独立文件、独立连接，随生命周期开闭（REFACTOR_PLAN §5.6）。
            async with open_checkpointer(
                checkpoint_database_path(resolved)
            ) as checkpointer:
                app.state.checkpointer = checkpointer
                # Agent 三端点的依赖与编译图只装配一次（stage5.md §4.3–§4.4）。
                app.state.agent_runtime = build_agent_runtime(db, checkpointer, resolved)
                app.state.business_timezone = local_timezone_name()
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
            # 与 /api/provider 同源：每次请求现算 provider.json ＋ MODEL_API_KEY，不缓存启动快照。
            "provider_has_api_key": provider_api_key_configured(
                request.app.state.data_dir
            ),
        }

    # 表单 API 路由必须先于 /{path:path} 静态兜底注册，否则会被前端宿主吞掉。
    dto.install_error_handlers(app)
    app.include_router(routes_profile.router)
    app.include_router(routes_records.router)
    app.include_router(routes_plans.router)
    app.include_router(routes_stats.router)
    app.include_router(routes_agent.router)
    app.include_router(routes_provider.router)
    _install_frontend_static(
        app,
        Path(frontend_dist) if frontend_dist is not None else frontend_dist_dir(),
    )
    return app


def build_agent_runtime(
    db: Database,
    checkpointer: BaseCheckpointSaver,
    data_dir: Path,
) -> routes_agent.AgentRuntime:
    """装配 Agent 三端点的生产依赖（stage5.md §4.2–§4.4）：一份 Planner／Evaluator 与唯一模型入口。

    同一个模型 callable 供 GeneratePlanDeps 与 AgentRunDeps 共用；``data_dir`` 是 provider.json
    的宿主目录。
    """
    model = _lazy_model_call(data_dir)
    deps = GeneratePlanDeps(
        profiles=ProfileService(db),
        catalog=ActionCatalogService(db),
        stats=StatsRepo(db),
        assembler=MemoryAssembler(db),
        skills=SkillLoader(),
        persistence=PlanPersistenceService(db),
        plans=PlanReadService(db),
        activation=PlanActivationService(db),
        model=model,
        now=lambda: datetime.now(UTC),
    )
    return routes_agent.AgentRuntime(
        graph=build_generate_plan_graph(deps, checkpointer=checkpointer),
        deps=deps,
        run_deps=AgentRunDeps(
            model=model,
            stats=StatsService(db),
            plans=deps.plans,
            persistence=deps.persistence,
            catalog=deps.catalog,
            records=WorkoutRecordsService(db),
        ),
    )


def _lazy_model_call(data_dir: Path) -> ModelCall:
    """唯一模型入口：每次调用重新 resolve 配置；Provider 异常换成固定文本 ModelCallFailed。"""

    async def configured_call(system_prompt: str, user_payload: str) -> str:
        try:
            call = openai_compatible_model_call(data_dir)
            return await call(system_prompt, user_payload)
        except ModelConfigurationError:
            raise
        except Exception as exc:
            raise ModelCallFailed(MODEL_CALL_FAILED_MESSAGE) from exc

    return configured_call


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
