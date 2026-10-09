import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import asynccontextmanager
from queue import Queue
from threading import Event, Lock
from time import time_ns
from traceback import walk_tb
from typing import Annotated
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from starlette.routing import compile_path
from starlette.types import ASGIApp, Receive, Scope, Send

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.events import AgentEvent
from app.agent.permissions import create_before_tool_call
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tool import CredentialDetectedError
from app.agent.tools.business import (
    bind_business_tools,
    business_tool_declarations,
)
from app.agent.tools.files import create_file_tools
from app.ai.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    text_projection,
)
from app.ai.types import check_cancelled
from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.service import (
    BUSINESS_TIMEZONE,
    BusinessService,
    business_date,
)
from app.application.session.attachment_files import AttachmentFiles
from app.application.session.service import CREDENTIAL_SAFE_MESSAGE, SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.business.errors import BusinessError
from app.domain.business.models import BusinessContext, WorkoutListArguments
from app.domain.session.attachments import (
    AttachmentError,
    AttachmentInput,
    attachment_storage_ref,
)
from app.domain.session.errors import (
    CredentialDetected,
    EntryNotFound,
    IncompleteToolChain,
    InvalidTargetEntry,
    OperationConflict,
    OperationExpired,
    RunBusy,
    RunClosed,
    RunNotFound,
    SessionConflict,
    SessionMismatch,
    SessionNotFound,
    SteeringConsumptionConflict,
)
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    OperationOutcome,
    RegenerateCommand,
    RegenerateRequest,
    SendCommand,
    SendRequest,
    SessionMessageEntry,
    SteeringCommand,
    SteeringInput,
)
from app.domain.session.models import (
    SteeringRequest as SteeringParams,
)
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.model_config import load_model_config

frontend_port = os.environ.get("FIT_AGENT_FRONTEND_PORT", "5173")
if re.fullmatch(r"[0-9]+", frontend_port) is None or not 1 <= int(frontend_port) <= 65535:
    raise ValueError("FIT_AGENT_FRONTEND_PORT 必须是 1–65535 的整数")
FRONTEND_PORT = int(frontend_port)
ALLOWED_HOSTS = {f"{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (8000, FRONTEND_PORT)}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
logger = logging.getLogger(__name__)
active = Lock()
runs_lock = Lock()
# 同键并发请求串行到受理提交：落败者等待后命中持久化结果。键随引用消失，不累积。
operation_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def operation_lock(operation_id: str) -> asyncio.Lock:
    return operation_locks.setdefault(operation_id, asyncio.Lock())


# 会话协调入口：新运行受理与会话删除在此串行，覆盖受理提交至内存登记间的竞态窗口。
session_gates: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def session_gate(session_id: str) -> asyncio.Lock:
    return session_gates.setdefault(session_id, asyncio.Lock())


TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)


class CredentialFilter:
    def __init__(self, secrets):
        self._secrets = tuple(secret for secret in secrets if secret)
        self._hold = max((len(secret) for secret in self._secrets), default=0) - 1

    def contains(self, value) -> bool:
        if isinstance(value, str):
            return any(secret in value for secret in self._secrets)
        if isinstance(value, list):
            return any(self.contains(item) for item in value)
        if isinstance(value, dict):
            return any(
                self.contains(key) or self.contains(item)
                for key, item in value.items()
            )
        return False

    def tail_safe(self, text: str) -> str:
        if self._hold <= 0:
            return text
        return text[: max(0, len(text) - self._hold)]


_NO_CREDENTIALS = CredentialFilter(())


def path_uuid(value: str) -> str:
    # 路径身份只接受标准带连字符 UUID；紧凑、URN 及花括号形式按非法请求拒绝。
    if not isinstance(value, str):
        raise ValueError("身份必须为字符串")
    canonical = str(UUID(value))
    if value.lower() != canonical:
        raise ValueError("路径身份必须为标准带连字符的 UUID")
    return canonical


PathUUID = Annotated[str, BeforeValidator(path_uuid)]


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: PathUUID
    title: str = Field(strict=True, min_length=1, max_length=32000)

    @field_validator("title")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title 必须包含非空文本")
        return value


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    request: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.request.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class EditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    target_entry_id: PathUUID
    request: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if "attachments" in self.model_fields_set and not self.request.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class RegeneratePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    target_entry_id: PathUUID


class SteeringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: PathUUID
    operation_id: PathUUID
    message: str = Field(max_length=32000)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.message.strip() and not self.attachments:
            raise ValueError("请求必须包含文字或附件")
        return self


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: PathUUID


class RunState:
    def __init__(self, session_id: UUID):
        self.run_id = uuid4()
        self.session_id = session_id
        self.operation_id = ""
        self.request_entry_id = ""
        self.cancel = Event()
        self.events: Queue[AgentEvent] = Queue()
        self.coordinator: SteeringCoordinator | None = None
        self.task: asyncio.Task | None = None
        self.closed_without_terminal = False

    def publish(self, name: str, data: dict) -> None:
        self.events.put(AgentEvent(name, {"run_id": str(self.run_id), **data}))

    def disconnect(self) -> None:
        self.cancel.set()
        if self.coordinator is not None:
            self.coordinator.close(str(self.run_id))


runs: dict[UUID, RunState] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    database = await open_database()
    service = SessionService(SqliteSessionRepository(database))
    await service.recover_interrupted()
    business_repository = SqliteBusinessRepository(database)
    catalog = Catalog.load()
    replacements = ReplacementCoordinator()
    business = BusinessService(business_repository, catalog, service, replacements)
    service.attach_snapshots(business)
    # 中断的保存在事务未提交时已随数据库回滚；恢复完成前不接收任何保存请求。
    await business_repository.recover_interrupted_saves()
    await business_repository.recover_interrupted_workout_saves()
    await business_repository.recover_interrupted_plan_saves()
    async with business_repository.transaction():
        await business_repository.replace_exercises(catalog.all())
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    app.state.session_service = service
    app.state.steering = SteeringCoordinator(service)
    app.state.business = business
    app.state.replacements = replacements
    app.state.loop = loop
    app.state.executor = executor
    app.state.coordination = set()
    app.state.accepting_runs = True
    try:
        yield
    finally:
        # 停止接收新运行，向已登记运行发出取消信号并关闭 Steering 接收入口。
        app.state.accepting_runs = False
        with runs_lock:
            states = list(runs.values())
        for state in states:
            state.cancel.set()
            if state.coordinator is not None:
                state.coordinator.close(str(state.run_id))
        # 异步排空收尾任务，覆盖与受理交错后刚登记的任务，期间主事件循环保持可调度。
        while True:
            with runs_lock:
                tasks = list(app.state.coordination)
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)
        await loop.run_in_executor(None, executor.shutdown, True)
        await database.close()


app = FastAPI(lifespan=lifespan)
NO_STORE = {"Cache-Control": "no-store"}


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    code = "invalid_request"
    if any(item["type"] == "attachment_format_invalid" or (
        item["type"] == "value_error" and "attachments" in item["loc"]
        and item["loc"][-1] == "data_base64"
    ) for item in error.errors()):
        code = "attachment_format_invalid"
    return JSONResponse(status_code=422, content={
        "detail": {"code": code, "message": "请求字段不合法。"},
    }, headers=NO_STORE)


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    return JSONResponse({"detail": error.detail}, error.status_code, headers=NO_STORE)


@app.exception_handler(BusinessError)
async def business_error(request: Request, error: BusinessError):
    detail = error.detail()
    if CredentialFilter((load_model_config().OPENAI_API_KEY,)).contains(detail):
        return JSONResponse({"detail": {
            "code": "credential_detected", "message": CREDENTIAL_SAFE_MESSAGE,
        }}, 422, headers=NO_STORE)
    return JSONResponse({"detail": detail}, error.http_status, headers=NO_STORE)


def stored_attachment_read_error(error: BaseException) -> bool:
    codes = {
        SqliteSessionRepository.get_attachments.__code__,
        SqliteSessionRepository.list_entry_attachments.__code__,
        SqliteSessionRepository.list_steering_attachments.__code__,
    }
    return any(frame.f_code in codes for frame, _ in walk_tb(error.__traceback__))


@app.exception_handler(ValueError)
async def attachment_metadata_error(request: Request, error: ValueError):
    if not stored_attachment_read_error(error):
        raise error
    return JSONResponse({"detail": {
        "code": "internal_error", "message": "服务内部错误。",
    }}, 500, headers=NO_STORE)


@app.exception_handler(AttachmentError)
async def attachment_error(request: Request, error: AttachmentError):
    code = error.code
    if code == "attachment_format_invalid" and (
        stored_attachment_read_error(error)
        or any(frame.f_code is AttachmentFiles.read.__code__ for frame, _ in walk_tb(error.__traceback__))
    ):
        code = "internal_error"
    status = {
        "invalid_request": 422, "attachment_format_invalid": 422,
        "attachment_size_exceeded": 413, "attachment_not_found": 404,
        "attachment_access_denied": 403, "attachment_conflict": 409,
        "internal_error": 500,
    }[code]
    return JSONResponse({"detail": {"code": code, "message": "附件操作失败。"}}, status, headers=NO_STORE)


@app.exception_handler(OSError)
@app.exception_handler(UnicodeDecodeError)
async def attachment_native_error(request: Request, error: OSError | UnicodeDecodeError):
    frames = {frame.f_code for frame, _ in walk_tb(error.__traceback__)}
    boundary = AttachmentFiles.read.__code__ in frames
    if isinstance(error, OSError):
        boundary = boundary or bool(frames & {
            AttachmentFiles.write.__code__, AttachmentFiles.prepare.__code__,
            AttachmentFiles.remove_created.__code__,
        })
    if not boundary:
        raise error
    return JSONResponse({"detail": {
        "code": "internal_error", "message": "服务内部错误。",
    }}, 500, headers=NO_STORE)


@app.exception_handler(Exception)
async def internal_error(request: Request, error: Exception):
    return JSONResponse(status_code=500, content={
        "detail": {"code": "internal_error", "message": "服务内部错误。"},
    }, headers=NO_STORE)


def reject(
    status: int, code: str, message: str, *, reason: str | None = None
) -> None:
    detail = {"code": code, "message": message}
    if reason is not None:
        detail["reason"] = reason
    raise HTTPException(status, detail)


def reject_operation_expired() -> None:
    reject(409, "operation_conflict", "该操作已失效。", reason="operation_expired")


async def reject_if_expired(
    service: SessionService, operation_id: str, session_id: str
) -> None:
    if await service.get_operation_invalidation(operation_id) == session_id:
        reject_operation_expired()


def encode(event: AgentEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False, allow_nan=False)}\n\n"


def public_content(message: AssistantMessage, secrets: CredentialFilter, truncate: bool) -> list[dict]:
    def text(value: str) -> str:
        return secrets.tail_safe(value) if truncate else value

    content = []
    for index, block in enumerate(message.content):
        if isinstance(block, TextContent):
            content.append({
                "content_index": index,
                "type": "text",
                "text": text(block.text),
            })
        elif isinstance(block, ThinkingContent) and not block.redacted:
            content.append({
                "content_index": index,
                "type": "thinking",
                "thinking": text(block.thinking),
            })
        elif isinstance(block, ToolCall):
            content.append({
                "content_index": index,
                "type": "tool_call",
                "tool_call_id": block.id,
                "name": block.name,
                "arguments": block.arguments,
            })
    return content


def public_tool_execution_end(event: dict) -> dict:
    # 内部完成事件到公开字段的映射：content 沿用工具结果的文本投影，不携带持久化节点身份。
    return {
        "tool_call_id": event["tool_call_id"],
        "tool_name": event["name"],
        "content": event["content"],
        "is_error": event["is_error"],
    }


def public_tool_execution_update(
    tool_call_id: str, tool_name: str, content: list, is_error: bool
) -> dict:
    # 进度快照：content 使用与最终结果一致的文本投影，前端直接替换；不携带持久化节点身份。
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "content": text_projection(content),
        "is_error": is_error,
    }


def public_message(message: Message) -> dict:
    if isinstance(message, SystemMessage):
        return {"role": "system"}
    if isinstance(message, UserMessage):
        return {
            "role": "user",
            "attachments": [],
            "text": text_projection(message.content),
            "timestamp": message.timestamp,
        }
    if isinstance(message, AssistantMessage):
        return {
            "role": "assistant",
            "content": public_content(message, _NO_CREDENTIALS, False),
            "stop_reason": message.stop_reason,
            "timestamp": message.timestamp,
        }
    if isinstance(message, ToolResultMessage):
        return {
            "role": "toolResult",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": text_projection(message.content),
            "is_error": message.is_error,
            "timestamp": message.timestamp,
        }
    raise ValueError("未知消息角色")


def attachment_object(metadata) -> dict:
    return {
        "attachment_id": metadata.attachment_id, "file_name": metadata.file_name,
        "size_bytes": metadata.size_bytes, "created_at": metadata.created_at,
    }


def entry_attachments(entry: SessionMessageEntry) -> list[dict]:
    # 真实用户节点的有序附件投影：读取路径相对统一 tmp 根，来源为后端元数据。
    return [
        {
            "attachment_id": item.attachment_id,
            "file_name": item.file_name,
            "path": attachment_storage_ref(
                item.session_id, item.attachment_id, item.file_name
            ).removeprefix("tmp/"),
        }
        for item in entry.attachments
    ]


def history_entry(entry: SessionMessageEntry) -> dict:
    message = public_message(entry.messages[0])
    if message["role"] == "user":
        message["attachments"] = [attachment_object(item) for item in entry.attachments]
    return {
        "entry_id": entry.id,
        "parent_id": entry.parent_id,
        "run_id": entry.run_id,
        "created_at": entry.created_at,
        "message": message,
    }


def history_steering(steering: SteeringInput) -> dict:
    return {
        "session_id": steering.session_id,
        "run_id": steering.run_id,
        "steering_id": steering.id,
        "text": text_projection(steering.message.content),
        "attachments": [attachment_object(item) for item in steering.attachments],
        "timestamp": steering.message.timestamp,
        "status": steering.status,
        "entry_id": steering.entry_id,
        "reason": steering.reason,
        "created_at": steering.created_at,
        "updated_at": steering.updated_at,
    }


def session_object(session) -> dict:
    return {
        "session_id": session.id,
        "title": session.title,
        "active_leaf_id": session.active_leaf_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def run_object(run) -> dict:
    return {
        "session_id": run.session_id,
        "run_id": run.id,
        "request_entry_id": run.request_entry_id,
        "last_entry_id": run.last_entry_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error_code": run.error_code,
        "error_message": run.error_message,
    }


def steering_object(steering) -> dict:
    return {
        "attachments": [attachment_object(item) for item in steering.attachments],
        "session_id": steering.session_id,
        "run_id": steering.run_id,
        "steering_id": steering.id,
        "status": steering.status,
        "entry_id": steering.entry_id,
        "reason": steering.reason,
        "created_at": steering.created_at,
        "updated_at": steering.updated_at,
    }


def send_result(operation_id: str, run) -> dict:
    return {
        "operation_id": operation_id,
        "session_id": run.session_id,
        "run_id": run.id,
        "request_entry_id": run.request_entry_id,
        "status": run.status,
    }


async def check_boundary(request: Request) -> None:
    hosts = request.headers.getlist("host")
    origins = request.headers.getlist("origin")
    if len(hosts) != 1 or hosts[0] not in ALLOWED_HOSTS:
        reject(403, "host_forbidden", "Host 不允许")
    if origins and (len(origins) != 1 or origins[0] not in ALLOWED_ORIGINS):
        reject(403, "origin_forbidden", "Origin 不允许")


class AttachmentBodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.steering_path = compile_path("/api/agent/runs/{run_id}/steering")[0]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        protected = scope["type"] == "http" and scope["method"] == "POST" and (
            scope["path"] in ("/api/agent/run", "/api/agent/edit")
            or self.steering_path.fullmatch(scope["path"]) is not None
        )
        if not protected:
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            await check_boundary(request)
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, error.status_code, headers=NO_STORE)(scope, receive, send)
            return
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1048576:
                await JSONResponse({"detail": {
                    "code": "request_size_exceeded", "message": "请求体大小超过限制。",
                }}, 413, headers=NO_STORE)(scope, receive, send)
                return
            body.extend(chunk)
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)


app.add_middleware(AttachmentBodyLimit)


async def require_run(service: SessionService, run_id: str, session_id: str) -> None:
    # 数据库为运行存在与会话归属的权威，重启后原键重试仍可定位。
    run = await service.find_run_any(str(run_id))
    if run is None:
        reject(404, "run_not_found", "运行不存在。")
    if run.session_id != str(session_id):
        reject(409, "session_mismatch", "运行与会话不匹配。")


def retire(state: RunState) -> None:
    with runs_lock:
        runs.pop(state.run_id, None)
    active.release()


# 计划域准备工具：成功结果持久化后均按 plan_prepared 登记绑定展示节点。
PLAN_PREPARE_TOOLS = {"prepare_plan", "prepare_plan_import", "prepare_plan_adjustment"}


def execute(
    run,
    session_id: str,
    branch: list[Message],
    tools: dict,
    state: RunState,
    service: SessionService,
    business: BusinessService,
    loop,
    model,
    secrets: tuple[str, ...],
) -> str:
    coordinator = state.coordinator
    guard = CredentialFilter(secrets)
    position = {"id": run.request_entry_id}
    parents: dict[str, str] = {}
    # 可信业务身份与日期：request_entry_id 随 Steering 消费更新，日期取该用户节点
    # 的真实 created_at，处理期间固定；每批按 source_entry_id 绑定。
    request_entry = {"id": run.request_entry_id}
    business_day = {"value": ""}
    # 准备调用按业务独立登记；结果节点提交后才绑定展示。
    prepared: dict[str, str] = {}
    workout_prepared: dict[str, str] = {}
    plan_prepared: dict[str, str] = {}

    def call(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def read_business_date(entry_id: str) -> str:
        entry = call(service.get_entry(session_id, entry_id))
        return business_date(entry.created_at)

    business_day["value"] = read_business_date(request_entry["id"])

    def business_context(source_entry_id: str) -> BusinessContext:
        return BusinessContext(
            timezone=BUSINESS_TIMEZONE,
            business_date=business_day["value"],
            session_id=session_id,
            run_id=run.id,
            request_entry_id=request_entry["id"],
            source_entry_id=source_entry_id,
        )

    def execution_registry(source_entry_id: str) -> dict:
        return {
            **tools,
            **bind_business_tools(
                business, business_context(source_entry_id), call, prepared,
                workout_prepared, plan_prepared
            ),
        }

    def message_nodes(leaf_id: str) -> list[dict]:
        # 当前消息路径的节点引用：保存工具需要真实展示及确认节点 ID，后端按此校验。
        nodes = []
        bindings = call(business.list_confirmation_bindings(session_id))
        displays = call(business.list_display_bindings(session_id))
        workout_bindings = call(business.list_workout_confirmation_bindings(session_id))
        workout_displays = call(business.list_workout_display_bindings(session_id))
        plan_bindings = call(business.list_plan_confirmation_bindings(session_id))
        plan_displays = call(business.list_plan_display_bindings(session_id))
        if (bindings.keys() & workout_bindings.keys()
                or (bindings.keys() | workout_bindings.keys()) & plan_bindings.keys()):
            raise RuntimeError("确认节点具有多类业务绑定")
        if (displays.keys() & workout_displays.keys()
                or (displays.keys() | workout_displays.keys()) & plan_displays.keys()):
            raise RuntimeError("展示节点具有多类业务绑定")
        for entry in call(service.get_branch(session_id, leaf_id)):
            message = entry.messages[0]
            if isinstance(message, UserMessage):
                node = {"entry_id": entry.id, "role": "user"}
                if entry.attachments:
                    # 附件只在真实用户节点投影，读取仍由后端元数据决定。
                    node["attachments"] = entry_attachments(entry)
                proposal_id = bindings.get(entry.id)
                if proposal_id is not None:
                    node["proposal_id"] = proposal_id
                    node["business_kind"] = "profile"
                workout_proposal_id = workout_bindings.get(entry.id)
                if workout_proposal_id is not None:
                    node["proposal_id"] = workout_proposal_id
                    node["business_kind"] = "workout"
                plan_proposal_id = plan_bindings.get(entry.id)
                if plan_proposal_id is not None:
                    node["proposal_id"] = plan_proposal_id
                    node["business_kind"] = "plan"
                nodes.append(node)
            elif isinstance(message, AssistantMessage):
                nodes.append(
                    {
                        "entry_id": entry.id,
                        "role": "assistant",
                        "tool_call_ids": [
                            block.id
                            for block in message.content
                            if isinstance(block, ToolCall)
                        ],
                    }
                )
            elif isinstance(message, ToolResultMessage):
                node = {
                    "entry_id": entry.id,
                    "role": "toolResult",
                    "tool_name": message.tool_name,
                    "tool_call_id": message.tool_call_id,
                }
                proposal_id = displays.get(entry.id)
                if proposal_id is not None:
                    node["proposal_id"] = proposal_id
                    node["display_entry_id"] = entry.id
                    node["business_kind"] = "profile"
                workout_proposal_id = workout_displays.get(entry.id)
                if workout_proposal_id is not None:
                    node["proposal_id"] = workout_proposal_id
                    node["display_entry_id"] = entry.id
                    node["business_kind"] = "workout"
                plan_proposal_id = plan_displays.get(entry.id)
                if plan_proposal_id is not None:
                    node["proposal_id"] = plan_proposal_id
                    node["display_entry_id"] = entry.id
                    node["business_kind"] = "plan"
                nodes.append(node)
        return nodes

    async def transform_context(messages: list[Message], signal) -> list[Message]:
        # 临时系统上下文：仅参与模型请求投影，不持久化，保持原提示词与工具声明完整。
        return [
            *messages,
            SystemMessage(
                role="system",
                content="",
                sections={
                    "business_context": json.dumps(
                        {
                            "timezone": BUSINESS_TIMEZONE,
                            "business_date": business_day["value"],
                            "request_entry_id": request_entry["id"],
                            "message_nodes": message_nodes(position["id"]),
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                },
                timestamp=time_ns() // 1_000_000,
            ),
        ]

    async def save_message(node_id: str, message: Message) -> None:
        parent_id = position["id"]

        async def save() -> object:
            return await service.append_entry(
                SessionMessageEntry(
                    session_id=session_id,
                    id=node_id,
                    parent_id=parent_id,
                    run_id=run.id,
                    type="message",
                    messages=[message],
                    created_at=time_ns() // 1_000_000,
                ),
                credentials=secrets,
            )

        outcome = asyncio.run_coroutine_threadsafe(save(), loop).result()
        if outcome.credential_detected:
            raise CredentialDetectedError()
        parents[node_id] = parent_id
        position["id"] = node_id
        if (
            isinstance(message, ToolResultMessage)
            and message.tool_name in {*PLAN_PREPARE_TOOLS, "prepare_profile_update", "prepare_workout"}
            and not message.is_error
        ):
            # 成功结果必须对应本批登记；关联缺失直接终止运行。
            if message.tool_name == "prepare_profile_update":
                call(business.bind_display_entry(prepared.pop(message.tool_call_id), node_id))
            elif message.tool_name == "prepare_workout":
                call(business.bind_workout_display_entry(
                    workout_prepared.pop(message.tool_call_id), node_id
                ))
            else:
                call(business.bind_plan_display_entry(
                    plan_prepared.pop(message.tool_call_id), node_id
                ))

    async def get_steering_messages() -> list[Message]:
        return call(coordinator.take(run.id))

    async def get_steering_messages_or_close() -> list[Message]:
        return call(coordinator.take_or_close(run.id))

    async def on_steering_consumed(messages: list[Message]) -> None:
        last_id = call(coordinator.consume(run.id, messages))
        if last_id is not None:
            position["id"] = last_id
            # Steering 消费后：后续批次的 request_entry_id 与业务日期使用新用户节点。
            request_entry["id"] = last_id
            business_day["value"] = read_business_date(last_id)

    async def emit(event) -> None:
        kind = event["type"]
        if kind in {"message_start", "message_update", "message_end"}:
            message = event["message"]
            if guard.contains(message.model_dump()):
                raise CredentialDetectedError()
            data = {
                "message_id": event["message_id"],
                "content": public_content(message, guard, kind != "message_end"),
            }
            if kind == "message_update":
                update = event["assistant_event"]
                index = update["content_index"]
                block = message.content[index]
                if isinstance(block, ThinkingContent) and block.redacted:
                    return
                data.update(content_index=index, update_type=update["type"])
            elif kind == "message_end":
                data["entry_id"] = event["message_id"]
                data["parent_id"] = parents.get(event["message_id"])
                data["stop_reason"] = message.stop_reason
            state.publish(kind, data)
        elif kind == "tool_start":
            data = {
                "tool_call_id": event["tool_call_id"],
                "name": event["name"],
                "arguments": event["arguments"],
            }
            if guard.contains(data):
                raise CredentialDetectedError()
            state.publish(kind, data)
        elif kind == "tool_result":
            data = {
                "tool_call_id": event["tool_call_id"],
                "content": event["content"],
                "is_error": event["is_error"],
                "entry_id": event["message_id"],
                "parent_id": parents.get(event["message_id"]),
            }
            if guard.contains(data):
                raise CredentialDetectedError()
            state.publish(kind, data)
        elif kind == "tool_execution_end":
            # 单工具结果定稿：经现有凭据检查后按实际完成顺序公开；不携带 entry_id、parent_id。
            data = public_tool_execution_end(event)
            if guard.contains(data):
                raise CredentialDetectedError()
            state.publish(kind, data)

    def on_tool_update(tool_call_id: str, tool_name: str, result) -> None:
        # 单工具进度快照：中间态，可被完成事件与保存确认覆盖；凭据在 harness 通知前拦截。
        state.publish(
            "tool_execution_update",
            public_tool_execution_update(
                tool_call_id, tool_name, result.content, result.is_error
            ),
        )

    loop_config = AgentLoopConfig(
        model=model,
        max_turns=64,
        transform_context=transform_context,
        get_steering_messages=get_steering_messages,
        get_steering_messages_or_close=get_steering_messages_or_close,
        on_steering_consumed=on_steering_consumed,
        save_message=save_message,
        bind_tools=execution_registry,
        before_tool_call=create_before_tool_call(business, call),
        on_tool_update=on_tool_update,
        contains_credentials=guard.contains,
    )
    # 稳定声明注册表：业务工具实例仅用于声明校验，实际执行按批重新绑定。
    context = {
        "messages": branch[:-1],
        "tools": execution_registry(run.request_entry_id),
    }
    messages = asyncio.run(
        run_agent_loop([branch[-1]], context, loop_config, emit, state.cancel)
    )
    check_cancelled(state.cancel)
    final_message = next(
        message for message in reversed(messages)
        if isinstance(message, AssistantMessage) and message.stop_reason in {"stop", "length"}
    )
    return final_message.stop_reason


def publish_terminal(state: RunState, run, stop_reason: str | None) -> None:
    # 公开终态由已提交运行记录决定，与数据库终态保持一致。
    if run.status == "completed":
        state.publish("done", {"status": "completed", "stop_reason": stop_reason})
        return
    cancelled = run.status == "cancelled"
    state.publish("error", {
        "status": run.status,
        "code": run.error_code or ("cancelled" if cancelled else "execution_failed"),
        "message": run.error_message
        or ("执行已取消。" if cancelled else "执行失败，请重新发起请求。"),
    })


def terminal_decision(
    failure: BaseException | None, cancelled: bool
) -> tuple[str, str | None, str | None]:
    # 凭据命中优先于取消；否则按取消、失败、完成的顺序裁决候选终态。
    if isinstance(failure, CredentialDetectedError):
        return "failed", "credential_detected", CREDENTIAL_SAFE_MESSAGE
    if cancelled:
        return "cancelled", "cancelled", "执行已取消。"
    if failure is not None:
        return "failed", "execution_failed", "执行失败，请重新发起请求。"
    return "completed", None, None


async def coordinate(
    run,
    state: RunState,
    session_id: str,
    tools: dict,
    model,
    secrets: tuple[str, ...],
    service: SessionService,
    business: BusinessService,
    coordinator: SteeringCoordinator,
    executor: ThreadPoolExecutor,
    loop,
) -> None:
    # 主事件循环持有的独立收尾任务：始终拥有已受理运行的终态所有权。
    failure: BaseException | None = None
    stop_reason: str | None = None
    try:
        branch = await service.get_context(session_id, run.request_entry_id)
        future = executor.submit(
            execute, run, session_id, branch, tools, state, service, business,
            loop, model, secrets,
        )
        stop_reason = await asyncio.wrap_future(future)
    except BaseException as error:
        failure = error

    cancelled = isinstance(failure, CancelledError) or state.cancel.is_set()
    status, code, message = terminal_decision(failure, cancelled)

    try:
        current = await service.get_run(session_id, run.id)
        if current.status in TERMINAL_RUN_STATUSES:
            # 已有终态保持原值，仍执行收尾以关闭接收入口并丢弃剩余输入。
            status, code, message = (
                current.status,
                current.error_code,
                current.error_message,
            )
        outcome = await coordinator.finish(
            session_id,
            run.id,
            status,
            error_code=code,
            error_message=message,
        )
    except BaseException:
        # 收尾读取或终态提交失败：结束当前 SSE，不发布已持久化终态确认，
        # 保留数据库真实状态及尚未完成收尾的运行占用。
        state.closed_without_terminal = True
        raise
    publish_terminal(state, outcome.run, stop_reason)
    retire(state)


def track(application: FastAPI, task: asyncio.Task) -> None:
    coordination = application.state.coordination
    coordination.add(task)

    def finalized(completed: asyncio.Task) -> None:
        coordination.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            # 仅记录受控诊断字段；禁止异常正文与 traceback 进入日志。
            logger.error(
                "运行收尾任务异常（%s），保留数据库真实状态。",
                type(error).__name__,
            )

    task.add_done_callback(finalized)


def stream(request: Request, state: RunState) -> AsyncIterator[str]:
    async def generate() -> AsyncIterator[str]:
        tool_call_id = None
        while True:
            if await request.is_disconnected():
                state.disconnect()
                return
            while not state.events.empty():
                if await request.is_disconnected():
                    state.disconnect()
                    return
                event = state.events.get_nowait()
                if event.event == "tool_start":
                    tool_call_id = event.data["tool_call_id"]
                elif event.event == "tool_result":
                    tool_call_id = None
                elif event.event == "error":
                    event.data["tool_call_id"] = tool_call_id
                yield encode(event)
                if event.event in {"done", "error"}:
                    return
            if state.closed_without_terminal:
                return
            await asyncio.sleep(0.01)

    return generate()


class RunStream(StreamingResponse):
    def __init__(self, state: RunState, body: AsyncIterator[str]):
        super().__init__(body, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-ID": str(state.session_id),
            "X-Operation-ID": state.operation_id,
            "X-Run-ID": str(state.run_id),
            "X-Request-Entry-ID": state.request_entry_id,
        })
        self.state = state

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.state.disconnect()


@app.post("/api/sessions", dependencies=[Depends(check_boundary)])
async def create_session(payload: CreateSessionRequest):
    service: SessionService = app.state.session_service
    credentials = (load_model_config().OPENAI_API_KEY,)
    try:
        created, session = await service.create_session_result(
            str(payload.session_id), payload.title, credentials=credentials
        )
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except SessionConflict:
        reject(409, "session_conflict", "会话标题冲突。")
    return JSONResponse(session_object(session), status_code=201 if created else 200)


@app.get("/api/sessions", dependencies=[Depends(check_boundary)])
async def list_sessions():
    service: SessionService = app.state.session_service
    guard = CredentialFilter((load_model_config().OPENAI_API_KEY,))
    sessions = await service.list_sessions()
    ordered = sorted(
        sessions, key=lambda session: (session.updated_at, session.id), reverse=True
    )
    payload = {"sessions": [session_object(session) for session in ordered]}
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def drain_session_runs(session_id: str) -> None:
    # 对目标会话的运行发出协作取消，等待执行任务与持久化收尾真正退出后返回。
    with runs_lock:
        states = [
            state for state in runs.values() if state.session_id == UUID(session_id)
        ]
    for state in states:
        state.disconnect()
    await asyncio.gather(*(state.task for state in states))


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(check_boundary)])
async def delete_session(session_id: PathUUID, request: Request):
    service: SessionService = request.app.state.session_service
    replacements: ReplacementCoordinator = request.app.state.replacements
    if CredentialFilter((load_model_config().OPENAI_API_KEY,)).contains(session_id):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)

    async def remove() -> None:
        # 串行新运行受理并登记删除意图，等待目标执行与收尾退出后执行删除事务。
        async with session_gate(session_id), replacements.register(session_id):
            await drain_session_runs(session_id)
            await service.delete_session(session_id)

    task = asyncio.get_running_loop().create_task(remove())
    track(request.app, task)
    # 删除任务的生命周期独立于 HTTP 连接：断连取消不影响服务端继续完成删除。
    await asyncio.shield(task)
    return JSONResponse({"session_id": session_id, "deleted": True}, headers=NO_STORE)


@app.get("/api/profile", dependencies=[Depends(check_boundary)])
async def get_profile():
    business: BusinessService = app.state.business
    guard = CredentialFilter((load_model_config().OPENAI_API_KEY,))
    response = await business.get_profile()
    payload = response.model_dump()
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


class WorkoutQuery(WorkoutListArguments):
    model_config = ConfigDict(extra="forbid", strict=False, allow_inf_nan=False)


def check_workout_query(request: Request, allowed: set[str]) -> None:
    keys = list(request.query_params.keys())
    if any(key not in allowed or len(request.query_params.getlist(key)) != 1 for key in keys):
        reject(422, "invalid_request", "请求字段不合法。")


def workout_response(response) -> JSONResponse:
    payload = response.model_dump()
    if CredentialFilter((load_model_config().OPENAI_API_KEY,)).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get("/api/plans/current", dependencies=[Depends(check_boundary)])
async def get_current_plan(request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.get_current_plan())


@app.get("/api/plans", dependencies=[Depends(check_boundary)])
async def list_plans(request: Request):
    check_workout_query(request, set())
    payload = [record.model_dump() for record in await request.app.state.business.list_plans()]
    if CredentialFilter((load_model_config().OPENAI_API_KEY,)).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get("/api/plans/{plan_id}", dependencies=[Depends(check_boundary)])
async def get_plan(plan_id: PathUUID, request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.get_plan(plan_id))


@app.get("/api/workouts", dependencies=[Depends(check_boundary)])
async def list_workouts(request: Request, query: Annotated[WorkoutQuery, Query()]):
    check_workout_query(request, {"date_from", "date_to", "page", "page_size"})
    arguments = WorkoutListArguments.model_validate(query.model_dump())
    return workout_response(await request.app.state.business.list_workouts(arguments))


@app.get("/api/workouts/{workout_id}", dependencies=[Depends(check_boundary)])
async def get_workout(workout_id: PathUUID, request: Request):
    check_workout_query(request, set())
    return workout_response(await request.app.state.business.get_workout(workout_id))


@app.get("/api/sessions/{session_id}/attachments/{attachment_id}", dependencies=[Depends(check_boundary)])
async def get_attachment(session_id: PathUUID, attachment_id: PathUUID, request: Request):
    check_workout_query(request, set())
    credentials = (load_model_config().OPENAI_API_KEY,)
    if CredentialFilter(credentials).contains({"session_id": session_id, "attachment_id": attachment_id}):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    try:
        metadata, text = await request.app.state.session_service.get_attachment_content(
            session_id, attachment_id, credentials=credentials,
        )
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {**attachment_object(metadata), "text": text}
    if CredentialFilter(credentials).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get(
    "/api/sessions/{session_id}/history",
    dependencies=[Depends(check_boundary)],
)
async def get_history(session_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    guard = CredentialFilter((load_model_config().OPENAI_API_KEY,))
    try:
        history = await service.get_session_history(str(session_id))
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    # 投影前先检查完整原始记录：系统内容、隐藏块、签名、嵌套元数据及身份字段。
    if guard.contains(
        {
            "session": history.session.model_dump(),
            "entries": [entry.model_dump() for entry in history.entries],
            "runs": [run.model_dump() for run in history.runs],
            "steering": [item.model_dump() for item in history.steering],
        }
    ):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "session": session_object(history.session),
        "entries": [history_entry(entry) for entry in history.entries],
        "runs": [run_object(run) for run in history.runs],
        "steering": [history_steering(item) for item in history.steering],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def resolve_existing(
    service: SessionService,
    operation_id: str,
    session_id: str,
    kind: str,
    request: object,
) -> OperationOutcome | None:
    # 先按 operation_id 查重：已失效编号返回冲突，已受理操作返回持久化原结果。
    try:
        outcome = await service.resolve_operation(
            operation_id, session_id, kind, request,
            credentials=(load_model_config().OPENAI_API_KEY,),
        )
        if outcome is not None and CredentialFilter((load_model_config().OPENAI_API_KEY,)).contains(outcome.model_dump()):
            reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
        return outcome
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except OperationExpired:
        reject_operation_expired()
    except OperationConflict:
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")


async def launch_run(
    request: Request,
    *,
    session_id: str,
    operation_id: str,
    accept: Callable[..., Awaitable[OperationOutcome]],
):
    service: SessionService = request.app.state.session_service
    coordinator: SteeringCoordinator = request.app.state.steering
    loop = asyncio.get_running_loop()
    # 新操作再检查关闭状态：关闭期间返回 run_busy，不创建记录、不调度线程。
    if not request.app.state.accepting_runs:
        reject(409, "run_busy", "服务正在关闭，暂不接受新运行。")

    if not active.acquire(blocking=False):
        reject(409, "run_busy", "已有任务正在执行")

    submitted = False
    try:
        disconnected = await request.is_disconnected()
        await service.get_session(session_id)
        model = load_model_config()
        secrets = (model.OPENAI_API_KEY,)
        tools = create_file_tools(session_id)
        outcome = await accept(service, tools, secrets)
        if not outcome.created:
            active.release()
            return JSONResponse(send_result(operation_id, outcome.run))
        run = outcome.run
        state = RunState(UUID(session_id))
        state.run_id = UUID(run.id)
        state.operation_id = operation_id
        state.request_entry_id = run.request_entry_id
        state.coordinator = coordinator
        coordinator.open(
            session_id, run.id, lambda data: state.publish("steering_status", data)
        )
        task = loop.create_task(coordinate(
            run, state, session_id, tools, model, secrets,
            service, request.app.state.business, coordinator,
            request.app.state.executor, loop,
        ))
        # 运行登记与独立收尾任务登记同关闭快照互斥，确保已受理运行必被等待。
        state.task = task
        with runs_lock:
            runs[state.run_id] = state
            track(request.app, task)
            closing = not request.app.state.accepting_runs
        # 断连或关闭只设置取消信号并关闭接收入口，收尾任务继续完成收尾。
        if disconnected or closing:
            state.cancel.set()
            coordinator.close(run.id)
        submitted = True
        return RunStream(state, stream(request, state))
    except OperationExpired:
        active.release()
        reject_operation_expired()
    except CredentialDetected:
        active.release()
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    except RunBusy:
        active.release()
        reject(409, "run_busy", "已有任务正在执行。")
    except InvalidTargetEntry:
        active.release()
        reject(409, "invalid_target_entry", "目标节点必须是用户消息。")
    except EntryNotFound:
        active.release()
        reject(404, "entry_not_found", "指定会话中找不到目标节点。")
    except IncompleteToolChain:
        active.release()
        reject(409, "incomplete_tool_chain", "执行分支存在未配对的工具调用。")
    except OperationConflict:
        active.release()
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        active.release()
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        active.release()
        reject(404, "session_not_found", "会话不存在。")
    except BaseException:
        if not submitted:
            active.release()
        raise


@app.post("/api/agent/run", dependencies=[Depends(check_boundary)])
async def run(payload: RunRequest, request: Request):
    check_workout_query(request, set())
    service: SessionService = request.app.state.session_service
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    send_request = SendRequest(text=payload.request, attachments=payload.attachments)

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        system_message = SystemMessage(
            role="system", content=SYSTEM_PROMPT,
            tools_added=[
                tool.definition() for tool in tools.values()
            ] + business_tool_declarations(),
            timestamp=time_ns() // 1_000_000,
        )
        return await svc.accept_send(
            SendCommand(
                operation_id=operation_id,
                session_id=session_id,
                request=send_request,
            ),
            system_message=system_message,
            credentials=secrets,
        )

    async with operation_lock(operation_id), session_gate(session_id):
        existing = await resolve_existing(
            service, operation_id, session_id, "send", send_request
        )
        if existing is not None:
            return JSONResponse(send_result(operation_id, existing.run))
        return await launch_run(
            request, session_id=session_id, operation_id=operation_id, accept=accept
        )


@app.post("/api/agent/edit", dependencies=[Depends(check_boundary)])
async def edit(payload: EditPayload, request: Request):
    check_workout_query(request, set())
    service: SessionService = request.app.state.session_service
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    edit_request = EditRequest(
        target_entry_id=str(payload.target_entry_id), text=payload.request,
        **({"attachments": payload.attachments} if "attachments" in payload.model_fields_set else {}),
    )

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        replacements: ReplacementCoordinator = request.app.state.replacements
        # 内容替换登记会话替换意图：确认提交在事务提交前检查该意图并让位。
        async with replacements.register(session_id):
            return await svc.accept_edit(
                EditCommand(
                    operation_id=operation_id,
                    session_id=session_id,
                    request=edit_request,
                ),
                credentials=secrets,
            )

    async with operation_lock(operation_id), session_gate(session_id):
        existing = await resolve_existing(
            service, operation_id, session_id, "edit", edit_request
        )
        if existing is not None:
            return JSONResponse(send_result(operation_id, existing.run))
        return await launch_run(
            request, session_id=session_id, operation_id=operation_id, accept=accept
        )


@app.post("/api/agent/regenerate", dependencies=[Depends(check_boundary)])
async def regenerate(payload: RegeneratePayload, request: Request):
    check_workout_query(request, set())
    service: SessionService = request.app.state.session_service
    session_id = str(payload.session_id)
    operation_id = str(payload.operation_id)
    regenerate_request = RegenerateRequest(
        target_entry_id=str(payload.target_entry_id)
    )

    async def accept(svc: SessionService, tools: dict, secrets: tuple[str, ...]):
        replacements: ReplacementCoordinator = request.app.state.replacements
        async with replacements.register(session_id):
            return await svc.accept_regenerate(
                RegenerateCommand(
                    operation_id=operation_id,
                    session_id=session_id,
                    request=regenerate_request,
                ),
                credentials=secrets,
            )

    async with operation_lock(operation_id), session_gate(session_id):
        existing = await resolve_existing(
            service, operation_id, session_id, "regenerate", regenerate_request
        )
        if existing is not None:
            return JSONResponse(send_result(operation_id, existing.run))
        return await launch_run(
            request, session_id=session_id, operation_id=operation_id, accept=accept
        )


@app.post("/api/agent/runs/{run_id}/steering", dependencies=[Depends(check_boundary)])
async def steering(run_id: PathUUID, payload: SteeringRequest, request: Request):
    check_workout_query(request, set())
    coordinator: SteeringCoordinator = app.state.steering
    service: SessionService = app.state.session_service
    steering_request = SteeringParams(target_run_id=run_id, text=payload.message, attachments=payload.attachments)
    existing = await resolve_existing(
        service, payload.operation_id, payload.session_id, "steering", steering_request,
    )
    if existing is None:
        await require_run(service, run_id, payload.session_id)
    credentials = (load_model_config().OPENAI_API_KEY,)
    try:
        created, steering_input = await coordinator.accept(
            str(run_id),
            SteeringCommand(
                operation_id=str(payload.operation_id),
                session_id=str(payload.session_id),
                request=steering_request,
            ),
            credentials=credentials,
        )
    except RunClosed:
        reject(409, "run_closed", "运行已关闭。")
    except OperationExpired:
        reject_operation_expired()
    except OperationConflict:
        reject(409, "operation_conflict", "操作请求冲突。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        reject(404, "run_not_found", "运行不存在。")
    except CredentialDetected:
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    if CredentialFilter(credentials).contains(steering_input.model_dump()):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return {
        "operation_id": str(payload.operation_id),
        "session_id": str(payload.session_id),
        "run_id": str(run_id),
        "steering_id": steering_input.id,
        "created": created,
        "status": "accepted" if created else steering_input.status,
        "entry_id": steering_input.entry_id,
        "reason": steering_input.reason,
    }


@app.post(
    "/api/agent/runs/{run_id}/steering/{steering_id}/withdraw",
    dependencies=[Depends(check_boundary)],
)
async def withdraw(run_id: PathUUID, steering_id: PathUUID, payload: WithdrawRequest, request: Request):
    check_workout_query(request, set())
    coordinator: SteeringCoordinator = app.state.steering
    await require_run(app.state.session_service, run_id, payload.session_id)
    try:
        withdrawal = await coordinator.withdraw(
            str(run_id), str(payload.session_id), str(steering_id)
        )
    except SteeringConsumptionConflict:
        reject(409, "steering_consumption_conflict", "目标输入消费已开始。")
    except SessionMismatch:
        reject(409, "session_mismatch", "运行与会话不匹配。")
    except SessionNotFound:
        reject(404, "steering_not_found", "输入不存在。")
    steering_input = withdrawal.steering
    if CredentialFilter((load_model_config().OPENAI_API_KEY,)).contains(steering_input.model_dump()):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return {
        "session_id": str(payload.session_id),
        "run_id": str(run_id),
        "steering_id": steering_input.id,
        "status": steering_input.status,
        "entry_id": steering_input.entry_id,
        "reason": steering_input.reason,
    }


@app.get(
    "/api/sessions/{session_id}/operations/{operation_id}",
    dependencies=[Depends(check_boundary)],
)
async def get_operation(session_id: PathUUID, operation_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    try:
        outcome = await service.get_operation_outcome(session_id, operation_id)
    except OperationExpired:
        reject_operation_expired()
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    guard = CredentialFilter((load_model_config().OPENAI_API_KEY,))
    if guard.contains({"session_id": session_id, "operation_id": operation_id, "outcome": outcome.model_dump() if outcome is not None else None}):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "operation_id": operation_id,
        "session_id": session_id,
        "accepted": outcome is not None,
        "kind": outcome.operation.kind if outcome is not None else None,
        "run": run_object(outcome.run) if outcome is not None else None,
        "steering": steering_object(outcome.steering) if outcome is not None and outcome.steering is not None else None,
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get(
    "/api/sessions/{session_id}/runs/{run_id}",
    dependencies=[Depends(check_boundary)],
)
async def get_run(session_id: PathUUID, run_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    try:
        await service.get_session(str(session_id))
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    try:
        run = await service.get_run(str(session_id), str(run_id))
    except SessionNotFound:
        reject(404, "run_not_found", "运行不存在。")
    payload = run_object(run)
    if CredentialFilter((load_model_config().OPENAI_API_KEY,)).contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers=NO_STORE)


@app.get(
    "/api/sessions/{session_id}/runs",
    dependencies=[Depends(check_boundary)],
)
async def list_session_runs(session_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    guard = CredentialFilter((load_model_config().OPENAI_API_KEY,))
    try:
        stored = await service.list_runs(session_id)
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    payload = {
        "session_id": session_id,
        "runs": [run_object(run) for run in stored],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get(
    "/api/sessions/{session_id}/runs/{run_id}/steering",
    dependencies=[Depends(check_boundary)],
)
async def list_run_steering(session_id: PathUUID, run_id: PathUUID, request: Request):
    check_workout_query(request, set())
    service: SessionService = app.state.session_service
    guard = CredentialFilter((load_model_config().OPENAI_API_KEY,))
    try:
        stored = await service.list_steering(session_id, run_id)
    except RunNotFound:
        reject(404, "run_not_found", "运行不存在。")
    except SessionNotFound:
        reject(404, "session_not_found", "会话不存在。")
    # 投影前先检查完整原始记录：输入正文、内容块签名及嵌套元数据。
    if guard.contains([item.model_dump() for item in stored]):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    payload = {
        "session_id": session_id,
        "run_id": run_id,
        "steering": [history_steering(item) for item in stored],
    }
    if guard.contains(payload):
        reject(422, "credential_detected", CREDENTIAL_SAFE_MESSAGE)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})
