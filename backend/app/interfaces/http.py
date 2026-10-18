import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from app.agent.prompts import SYSTEM_PROMPT as SYSTEM_PROMPT
from app.agent.telemetry import connect_trace_store
from app.agent.tool import CredentialDetectedError as CredentialDetectedError
from app.application.business.catalog import Catalog
from app.application.business.confirmation import ConfirmationService
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.retrieval_runtime import ActionRetrievalRuntime
from app.application.business.service import BusinessService
from app.application.session.coordination import (
    COMPACTION_FAILED_MESSAGE as COMPACTION_FAILED_MESSAGE,
)
from app.application.session.coordination import (
    CONTEXT_BUDGET_EXCEEDED_MESSAGE as CONTEXT_BUDGET_EXCEEDED_MESSAGE,
)
from app.application.session.coordination import (
    CONTEXT_OVERFLOW_MESSAGE as CONTEXT_OVERFLOW_MESSAGE,
)
from app.application.session.coordination import (
    MODEL_OUTPUT_LIMIT_MESSAGE as MODEL_OUTPUT_LIMIT_MESSAGE,
)
from app.application.session.coordination import (
    PROVIDER_QUOTA_EXCEEDED_MESSAGE as PROVIDER_QUOTA_EXCEEDED_MESSAGE,
)
from app.application.session.coordination import (
    publish_terminal as publish_terminal,
)
from app.application.session.coordination import (
    retire as retire,
)
from app.application.session.coordination import (
    terminal_decision as terminal_decision,
)
from app.application.session.runtime import (
    accepting_runs,
    coordination,
    reset_fault_closure,
    runs,
    runs_lock,
)
from app.application.session.runtime import (
    active as active,
)
from app.application.session.runtime import (
    is_fault_closed as is_fault_closed,
)
from app.application.session.runtime import (
    session_run_limits as session_run_limits,
)
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.business.errors import BusinessError
from app.domain.session.attachments import AttachmentError
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http_api import errors
from app.interfaces.http_api.contracts import ProviderWriteBody as ProviderWriteBody
from app.interfaces.http_api.contracts import RunRequest as RunRequest
from app.interfaces.http_api.projections import (
    history_entry as history_entry,
)
from app.interfaces.http_api.projections import (
    public_content as public_content,
)
from app.interfaces.http_api.projections import (
    public_message as public_message,
)
from app.interfaces.http_api.projections import (
    public_text_visible as public_text_visible,
)
from app.interfaces.http_api.projections import (
    public_tool_execution_end as public_tool_execution_end,
)
from app.interfaces.http_api.projections import (
    public_tool_execution_update as public_tool_execution_update,
)
from app.interfaces.http_api.routes import agent, business, provider, sessions
from app.interfaces.http_api.security import (
    ALLOWED_HOSTS as ALLOWED_HOSTS,
)
from app.interfaces.http_api.security import (
    FRONTEND_PORT as FRONTEND_PORT,
)
from app.interfaces.http_api.security import (
    AttachmentBodyLimit,
    check_boundary,
)
from app.interfaces.http_api.stream import RunState as RunState
from app.model_config import ModelNotConfigured, saved_api_key


def retrieval_secret() -> str | None:
    # 检索密钥凭据保护：直接读环境变量，不触发检索配置校验、不打印，未配置时为空。
    value = os.environ.get("DASHSCOPE_API_KEY")
    return value if value and value.strip() else None


def active_secrets(session_id: str | None = None) -> tuple[str, ...]:
    # 合并当前保存的凭据与在途运行快照：清除或更换 Key 后，仍在使用的凭据继续参与检查。
    current = saved_api_key()
    with runs_lock:
        states = list(runs.values())
    held = [
        secret
        for state in states
        if session_id is None or str(state.session_id) == str(session_id)
        for secret in state.secrets
    ]
    return tuple(secret for secret in (current, retrieval_secret(), *held) if secret)


@asynccontextmanager
async def lifespan(app: FastAPI):
    reset_fault_closure()
    database = await open_database()
    repository = SqliteSessionRepository(database)
    service = SessionService(repository)
    await service.runs.recover_interrupted()
    business_repository = SqliteBusinessRepository(database)
    catalog = Catalog.load()
    replacements = ReplacementCoordinator()
    # 检索资源按需初始化：此处仅创建持有者，缺省配置或 Milvus 不可用只影响后续检索请求。
    retrieval = ActionRetrievalRuntime()
    business = BusinessService(business_repository, catalog, service.queries, replacements, retrieval)
    service.attach_snapshots(business.proposals)
    confirmations = ConfirmationService(
        business_repository, service.queries, service.operations, service.entries, business
    )
    # 中断的保存在事务未提交时已随数据库回滚；恢复完成前不接收任何保存请求。
    await business_repository.recover_interrupted_saves()
    await business_repository.recover_interrupted_workout_saves()
    await business_repository.recover_interrupted_plan_saves()
    # 确认对账排在业务保存恢复之后：核对处理中操作的保存记录，恢复 saved 或登记中断失败。
    await confirmations.recover_interrupted()
    async with business_repository.transaction():
        await business_repository.replace_exercises(catalog.all())
    # 观测连接在主事件循环获取：开库故障隔离为 None，业务服务沿用既有生命周期。
    trace_store = await connect_trace_store()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    app.state.trace_store = trace_store
    app.state.session_service = service
    app.state.session_queries = service.queries
    app.state.session_entries = service.entries
    app.state.session_runs = service.runs
    app.state.session_operations = service.operations
    app.state.session_attachments = service.attachments
    app.state.active_secrets = active_secrets
    app.state.retrieval_secret = retrieval_secret
    app.state.steering = SteeringCoordinator(service, service.operations, service.runs)
    app.state.business = business
    app.state.business_repository = business_repository
    app.state.confirmations = confirmations
    app.state.database = database
    app.state.replacements = replacements
    app.state.retrieval = retrieval
    app.state.loop = loop
    app.state.executor = executor
    coordination.clear()
    accepting_runs.set()
    try:
        yield
    finally:
        # 停止接收新运行，向已登记运行发出取消信号并关闭 Steering 接收入口。
        accepting_runs.clear()
        with runs_lock:
            states = list(runs.values())
        for state in states:
            state.control.request_cancel("cancelled")
            if state.coordinator is not None:
                state.coordinator.close(str(state.run_id))
        # 异步排空收尾任务，覆盖与受理交错后刚登记的任务，期间主事件循环保持可调度。
        while True:
            with runs_lock:
                tasks = list(coordination)
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)
        await loop.run_in_executor(None, executor.shutdown, True)
        # 运行与取消收尾排空后再在所属循环关闭检索客户端；随后关闭观测与业务库。
        await retrieval.close()
        if trace_store is not None:
            await trace_store.close()
        await database.close()


app = FastAPI(lifespan=lifespan)
app.add_exception_handler(RequestValidationError, errors.invalid_request)
app.add_exception_handler(HTTPException, errors.http_error)
app.add_exception_handler(BusinessError, errors.business_error)
app.add_exception_handler(ValueError, errors.attachment_metadata_error)
app.add_exception_handler(AttachmentError, errors.attachment_error)
app.add_exception_handler(OSError, errors.attachment_native_error)
app.add_exception_handler(UnicodeDecodeError, errors.attachment_native_error)
app.add_exception_handler(Exception, errors.internal_error)
app.add_exception_handler(ModelNotConfigured, errors.model_not_configured)
app.add_middleware(AttachmentBodyLimit)
app.include_router(provider.router, dependencies=[Depends(check_boundary)])
app.include_router(sessions.router, dependencies=[Depends(check_boundary)])
app.include_router(business.router, dependencies=[Depends(check_boundary)])
app.include_router(agent.router, dependencies=[Depends(check_boundary)])
