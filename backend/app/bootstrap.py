"""组合根：具体 Repository、Application Service 与 Agent Runtime 的唯一构造点。"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.api.app import create_fastapi_app
from app.application.agent.contracts import (
    AgentRunDeps,
    AgentRuntime,
    GeneratePlanDeps,
    Intent,
)
from app.application.agent.harness.cache import ToolResultCache
from app.application.agent.harness.tools.general import build_general_tool_harnesses
from app.application.agent.memory import MemoryAssembler
from app.application.agent.plan_graph import build_generate_plan_graph
from app.application.ports import (
    Conversations,
    ExerciseCatalog,
    HealthProbe,
    Plans,
)
from app.application.services.body_metrics_service import BodyMetricsService
from app.application.services.conversation_service import (
    INTERRUPTED_RUN_ERROR_CODE,
    ConversationService,
)
from app.application.services.plans_service import (
    PlanActivationService,
    PlanPersistenceService,
)
from app.application.services.profile_service import ProfileService
from app.application.services.records_service import WorkoutRecordsService
from app.application.services.stats_service import StatsService
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.actions_repository import ExerciseRepo
from app.infrastructure.database.repositories.body_metrics_repository import (
    BodyMetricsRepo,
)
from app.infrastructure.database.repositories.conversations_repository import (
    ConversationRepo,
)
from app.infrastructure.database.repositories.plans_repository import PlanRepo
from app.infrastructure.database.repositories.profile_repository import ProfileRepo
from app.infrastructure.database.repositories.records_repository import (
    WorkoutRecordsRepo,
)
from app.infrastructure.database.repositories.stats_repository import StatsRepo
from app.infrastructure.database.repositories.tool_cache_repository import (
    ToolCacheRevisionsRepo,
)
from app.infrastructure.langgraph.checkpoints import open_checkpointer
from app.infrastructure.llm.gateway import build_dynamic_model_gateway
from app.infrastructure.llm.provider_settings import provider_api_key_configured
from app.infrastructure.skills.loader import SkillLoader
from config import (
    TOOL_TIMEOUT_SECONDS,
    checkpoint_database_path,
    database_path,
    local_timezone_name,
    resolve_data_dir,
    skills_dir,
)


@dataclass(frozen=True, slots=True)
class ReadRepositories:
    """8 个具体 Repository：组合根唯一构造点，同时提供只读查询与 ``*_in_transaction`` 写原语。"""

    profiles: ProfileRepo
    exercises: ExerciseRepo
    plans: PlanRepo
    records: WorkoutRecordsRepo
    body_metrics: BodyMetricsRepo
    stats: StatsRepo
    tool_cache: ToolCacheRevisionsRepo
    conversations: ConversationRepo


@dataclass(frozen=True, slots=True)
class SqliteHealthProbe:
    """健康探针的具体实现：连接状态来自 Database，Provider 能力来自组合根持有的 data_dir。"""

    db: Database
    data_dir: Path

    def database_is_open(self) -> bool:
        return self.db.is_open

    def provider_has_api_key(self) -> bool:
        return provider_api_key_configured(self.data_dir)


@dataclass(frozen=True, slots=True)
class AppServices:
    """endpoint 的构造期依赖：Application Service ＋ 具体 Repository ＋ 事务入口 ＋ 健康探针。"""

    profile: ProfileService
    body_metrics: BodyMetricsService
    records: WorkoutRecordsService
    plan_persistence: PlanPersistenceService
    plan_activation: PlanActivationService
    stats: StatsService
    conversations: ConversationService
    plans: Plans
    exercises: ExerciseCatalog
    conversations_repo: Conversations
    db: Database
    health: HealthProbe


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
            await db.migrate()
            user_version = await db.pragma_value("user_version")
            if not isinstance(user_version, int):
                raise RuntimeError(f"迁移后 user_version 不是整数：{user_version!r}")
            repositories = build_repositories(db)
            services = build_services(
                repositories, db, SqliteHealthProbe(db, resolved)
            )
            app.state.services = services
            # 上次进程终止留下的未完成 Run 一律收敛为 failed，已提交 Run Event 原样保留供展示。
            await services.conversations.converge_unfinished_runs(
                error_code=INTERRUPTED_RUN_ERROR_CODE,
                updated_at=datetime.now(UTC).isoformat(),
            )
            async with open_checkpointer(
                checkpoint_database_path(resolved)
            ) as checkpointer:
                app.state.checkpointer = checkpointer
                app.state.agent_runtime = build_agent_runtime(
                    checkpointer,
                    data_dir=resolved,
                    schema_version=user_version,
                    repositories=repositories,
                    services=services,
                )
                app.state.business_timezone = local_timezone_name()
                yield
        finally:
            await db.close()

    return create_fastapi_app(lifespan=lifespan, frontend_dist=frontend_dist)


def build_repositories(db: Database) -> ReadRepositories:
    """Repository 的唯一构造点：数据库连接只在组合根进入 Repository。"""
    return ReadRepositories(
        profiles=ProfileRepo(db),
        exercises=ExerciseRepo(db),
        plans=PlanRepo(db),
        records=WorkoutRecordsRepo(db),
        body_metrics=BodyMetricsRepo(db),
        stats=StatsRepo(db),
        tool_cache=ToolCacheRevisionsRepo(db),
        conversations=ConversationRepo(db),
    )


def build_services(
    repositories: ReadRepositories, db: Database, health: HealthProbe
) -> AppServices:
    """Application Service 的唯一构造点：具体 Repository 与事务入口显式注入。"""
    return AppServices(
        profile=ProfileService(repositories.profiles, repositories.exercises),
        body_metrics=BodyMetricsService(
            repositories.body_metrics, repositories.tool_cache, db
        ),
        records=WorkoutRecordsService(
            repositories.records,
            repositories.exercises,
            repositories.tool_cache,
            db,
        ),
        plan_persistence=PlanPersistenceService(
            repositories.plans, repositories.tool_cache, db
        ),
        plan_activation=PlanActivationService(
            repositories.plans,
            repositories.profiles,
            repositories.exercises,
            repositories.stats,
            repositories.tool_cache,
            db,
        ),
        stats=StatsService(repositories.stats, repositories.plans),
        conversations=ConversationService(repositories.conversations),
        plans=repositories.plans,
        exercises=repositories.exercises,
        conversations_repo=repositories.conversations,
        db=db,
        health=health,
    )


def build_agent_runtime(
    checkpointer: BaseCheckpointSaver,
    *,
    data_dir: Path,
    schema_version: int,
    repositories: ReadRepositories,
    services: AppServices,
) -> AgentRuntime:
    """装配 Agent 三端点的生产依赖：一份 Planner／Evaluator、唯一模型入口与一次性创建的只读工具缓存。"""
    model = build_dynamic_model_gateway(data_dir)
    skills = SkillLoader(skills_dir())
    cache = ToolResultCache(
        revisions=repositories.tool_cache, schema_version=schema_version
    )
    tool_harnesses: Mapping[Intent, CompiledStateGraph] = build_general_tool_harnesses(
        timeout_seconds=TOOL_TIMEOUT_SECONDS, cache=cache
    )
    deps = GeneratePlanDeps(
        profiles=services.profile,
        catalog=repositories.exercises,
        stats=repositories.stats,
        assembler=MemoryAssembler(
            profiles=repositories.profiles,
            plans=repositories.plans,
            records=services.records,
            stats=services.stats,
        ),
        skills=skills,
        persistence=services.plan_persistence,
        plans=repositories.plans,
        activation=services.plan_activation,
        model=model,
        now=lambda: datetime.now(UTC),
    )
    return AgentRuntime(
        graph=build_generate_plan_graph(deps, checkpointer=checkpointer),
        deps=deps,
        run_deps=AgentRunDeps(
            model=model,
            stats=services.stats,
            plans=deps.plans,
            persistence=deps.persistence,
            catalog=deps.catalog,
            records=services.records,
            skills=skills,
            tool_harnesses=tool_harnesses,
        ),
    )
