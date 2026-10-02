import asyncio
import json
from collections.abc import AsyncIterator
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from queue import Queue
from threading import Event, Lock
from time import time_ns
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agent.agent_loop import run_agent_loop
from src.agent.config import AgentLoopConfig
from src.agent.events import AgentEvent
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools.bash import create_bash_tool
from src.agent.tools.files import create_file_tools
from src.ai.context import normalize_context
from src.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    UserMessage,
)
from src.ai.types import check_cancelled
from src.model_config import load_model_config

ALLOWED_HOSTS = {"127.0.0.1:8000", "localhost:8000", "127.0.0.1:5173", "localhost:5173"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
sessions: dict[UUID, list] = {}
active = Lock()
runs_lock = Lock()
closed_runs: dict[UUID, UUID] = {}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    request: str = Field(strict=True, min_length=1, max_length=32000)

    @field_validator("request")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request 必须包含非空文本")
        return value


class SteeringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    message: str = Field(strict=True, min_length=1, max_length=32000)

    @field_validator("message")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message 必须包含非空文本")
        return value


class RunState:
    def __init__(self, session_id: UUID):
        self.run_id = uuid4()
        self.session_id = session_id
        self.cancel = Event()
        self.events: Queue[AgentEvent] = Queue()
        self.steering: Queue[UserMessage] = Queue()
        self.unconsumed: dict[int, tuple[str, UserMessage]] = {}
        self.accepting = True

    def publish(self, name: str, data: dict) -> None:
        self.events.put(AgentEvent(name, {"run_id": str(self.run_id), **data}))

    def close(self, reason: str) -> None:
        self.accepting = False
        for steering_id, _ in self.unconsumed.values():
            self.publish("steering_status", {
                "steering_id": steering_id, "status": "discarded", "reason": reason,
            })
        self.unconsumed.clear()
        while not self.steering.empty():
            self.steering.get_nowait()

    def disconnect(self) -> None:
        with runs_lock:
            self.cancel.set()
            self.accepting = False

    async def drain(self, close_if_empty: bool = False) -> list[UserMessage]:
        with runs_lock:
            check_cancelled(self.cancel)
            messages = []
            while not self.steering.empty():
                messages.append(self.steering.get_nowait())
            if close_if_empty and not messages:
                self.accepting = False
            return messages

    async def drain_or_close(self) -> list[UserMessage]:
        return await self.drain(close_if_empty=True)

    async def consumed(self, messages: list) -> None:
        with runs_lock:
            for message in messages:
                steering_id, _ = self.unconsumed.pop(id(message))
                self.publish("steering_status", {
                    "steering_id": steering_id, "status": "consumed",
                })


runs: dict[UUID, RunState] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    with ThreadPoolExecutor(max_workers=1) as executor:
        app.state.executor = executor
        yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    return JSONResponse(status_code=422, content={
        "detail": {"code": "invalid_request", "message": "请求字段不合法。"},
    })


def reject(status: int, code: str, message: str) -> None:
    raise HTTPException(status, {"code": code, "message": message})


def encode(event: AgentEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False, allow_nan=False)}\n\n"


def public_content(message: AssistantMessage) -> list[dict]:
    content = []
    for index, block in enumerate(message.content):
        if isinstance(block, TextContent):
            content.append({"content_index": index, "type": "text", "text": block.text})
        elif isinstance(block, ThinkingContent) and not block.redacted:
            content.append({"content_index": index, "type": "thinking", "thinking": block.thinking})
        elif isinstance(block, ToolCall):
            content.append({
                "content_index": index, "type": "tool_call",
                "tool_call_id": block.id, "name": block.name, "arguments": block.arguments,
            })
    return content


def redact(value, secret: str):
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact(key, secret): redact(item, secret) for key, item in value.items()}
    return value


async def check_boundary(request: Request) -> None:
    hosts = request.headers.getlist("host")
    origins = request.headers.getlist("origin")
    if len(hosts) != 1 or hosts[0] not in ALLOWED_HOSTS:
        reject(403, "host_forbidden", "Host 不允许")
    if origins and (len(origins) != 1 or origins[0] not in ALLOWED_ORIGINS):
        reject(403, "origin_forbidden", "Origin 不允许")


@app.post("/api/agent/runs/{run_id}/steering", dependencies=[Depends(check_boundary)])
async def steering(run_id: UUID, payload: SteeringRequest):
    with runs_lock:
        state = runs.get(run_id)
        session_id = state.session_id if state is not None else closed_runs.get(run_id)
        if session_id is None:
            reject(404, "run_not_found", "运行不存在。")
        if session_id != payload.session_id:
            reject(409, "session_mismatch", "运行与会话不匹配。")
        if state is None or not state.accepting:
            reject(409, "run_closed", "运行已关闭。")
        message = UserMessage(role="user", content=payload.message, timestamp=time_ns() // 1_000_000)
        steering_id = str(uuid4())
        state.unconsumed[id(message)] = (steering_id, message)
        state.steering.put(message)
        return {"run_id": str(run_id), "steering_id": steering_id, "status": "accepted"}


@app.post("/api/agent/run", dependencies=[Depends(check_boundary)])
async def run(payload: RunRequest, request: Request):
    if not active.acquire(blocking=False):
        reject(409, "run_busy", "已有任务正在执行")
    state = RunState(payload.session_id)
    with runs_lock:
        runs[state.run_id] = state
    new_session = payload.session_id not in sessions

    def execute() -> str:
        model = load_model_config()
        tools = {**create_file_tools(), "bash": create_bash_tool()}
        session = deepcopy(sessions.get(payload.session_id, []))
        timestamp = time_ns() // 1_000_000
        user = UserMessage(role="user", content=payload.request, timestamp=timestamp)
        if new_session:
            session.insert(0, SystemMessage(
                role="system", content=SYSTEM_PROMPT,
                tools_added=[tool.definition() for tool in tools.values()], timestamp=timestamp,
            ))
        loop_config = AgentLoopConfig(
            model=model, max_turns=64, get_steering_messages=state.drain,
            get_steering_messages_or_close=state.drain_or_close,
            on_steering_consumed=state.consumed,
        )
        context = {"messages": session, "tools": tools}
        message_id = None

        async def emit(event) -> None:
            nonlocal message_id
            kind = event["type"]
            if kind in {"message_start", "message_update", "message_end"}:
                if kind == "message_start":
                    message_id = str(uuid4())
                message = event["message"]
                data = {"message_id": message_id, "content": public_content(message)}
                if kind == "message_update":
                    update = event["assistant_event"]
                    index = update["content_index"]
                    block = message.content[index]
                    if isinstance(block, ThinkingContent) and block.redacted:
                        return
                    data.update(content_index=index, update_type=update["type"])
                elif kind == "message_end":
                    data["stop_reason"] = message.stop_reason
                state.publish(kind, redact(data, model.OPENAI_API_KEY))
            elif kind == "tool_start":
                state.publish(kind, redact({
                    "tool_call_id": event["tool_call_id"], "name": event["name"],
                    "arguments": event["arguments"],
                }, model.OPENAI_API_KEY))
            elif kind == "tool_result":
                state.publish(kind, redact({
                    "tool_call_id": event["tool_call_id"], "content": event["content"],
                    "is_error": event["is_error"],
                }, model.OPENAI_API_KEY))

        new_messages = asyncio.run(run_agent_loop([user], context, loop_config, emit, state.cancel))
        check_cancelled(state.cancel)
        final_message = next(
            message for message in reversed(new_messages)
            if isinstance(message, AssistantMessage) and message.stop_reason in {"stop", "length"}
        )
        history = normalize_context([*session, *new_messages])
        with runs_lock:
            check_cancelled(state.cancel)
            sessions[payload.session_id] = history
        return final_message.stop_reason

    def finished(future) -> None:
        failure = future.exception()
        with runs_lock:
            cancelled = isinstance(failure, CancelledError) or state.cancel.is_set()
            reason = "cancelled" if cancelled else "run_failed" if failure is not None else "completed"
            state.close(reason)
            if failure is None:
                state.publish("done", {"status": "completed", "stop_reason": future.result()})
            else:
                state.publish("error", {
                    "status": "cancelled" if cancelled else "failed",
                    "code": "cancelled" if cancelled else "execution_failed",
                    "message": "执行已取消。" if cancelled else "执行失败，请重新发起请求。",
                })
            closed_runs[state.run_id] = state.session_id
            del runs[state.run_id]
            active.release()

    future = request.app.state.executor.submit(execute)
    future.add_done_callback(finished)

    async def stream() -> AsyncIterator[str]:
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
            await asyncio.sleep(0.01)

    class RunStream(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                state.disconnect()

    return RunStream(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": str(state.run_id),
    })
