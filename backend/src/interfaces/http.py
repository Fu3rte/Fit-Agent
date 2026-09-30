import asyncio
import json
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from functools import partial
from queue import Queue
from threading import Event, Lock
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agent.events import AgentEvent
from src.agent.loop import run_turn
from src.agent.prompts import SYSTEM_PROMPT
from src.agent.providers.openai import complete
from src.agent.tools.bash import create_bash_tool
from src.agent.tools.files import create_file_tools
from src.model_config import load_model_config

ALLOWED_HOSTS = {"127.0.0.1:8000", "localhost:8000", "127.0.0.1:5173", "localhost:5173"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
sessions: dict[UUID, list[dict]] = {}
active = Lock()


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    with ThreadPoolExecutor(max_workers=1) as executor:
        app.state.executor = executor
        yield


app = FastAPI(lifespan=lifespan)


def encode(event: AgentEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


async def check_boundary(request: Request) -> None:
    hosts = request.headers.getlist("host")
    origins = request.headers.getlist("origin")
    if len(hosts) != 1 or hosts[0] not in ALLOWED_HOSTS:
        raise HTTPException(403, "Host 不允许")
    if origins and (len(origins) != 1 or origins[0] not in ALLOWED_ORIGINS):
        raise HTTPException(403, "Origin 不允许")


@app.post("/api/agent/run", dependencies=[Depends(check_boundary)])
async def run(payload: RunRequest, request: Request):
    if not active.acquire(blocking=False):
        raise HTTPException(409, "已有任务正在执行")
    cancel = Event()
    events: Queue[AgentEvent] = Queue()
    pending = deepcopy(
        sessions.get(payload.session_id, [{"role": "system", "content": SYSTEM_PROMPT}])
    )
    pending.append({"role": "user", "content": payload.request})

    def execute() -> None:
        config = load_model_config()
        with config.create_client() as client:
            provider = partial(complete, client, config.OPENAI_MODEL)
            for event in run_turn(
                pending,
                provider,
                {**create_file_tools(), "bash": create_bash_tool()},
                64,
                cancel,
            ):
                if event.event != "done":
                    events.put(event)
        if cancel.is_set():
            return
        sessions[payload.session_id] = pending
        events.put(AgentEvent("done", {"status": "completed"}))

    future = request.app.state.executor.submit(execute)
    future.add_done_callback(lambda _: active.release())

    async def stream() -> AsyncIterator[str]:
        tool_call_id = None
        while True:
            if await request.is_disconnected():
                return
            while not events.empty():
                event = events.get_nowait()
                if event.event == "tool_start":
                    tool_call_id = event.data["tool_call_id"]
                elif event.event == "tool_result":
                    tool_call_id = None
                yield encode(event)
                if event.event == "done":
                    return
            if future.done():
                if not events.empty():
                    continue
                if future.exception() is not None:
                    yield encode(AgentEvent("error", {
                        "message": "执行失败，请重新发起请求。",
                        "tool_call_id": tool_call_id,
                    }))
                return
            await asyncio.sleep(0.01)

    class RunStream(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                cancel.set()

    return RunStream(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
