import os
import socket


def _free_port(start: int) -> int:
    for port in range(start, start + 200):
        if not _any_listening(port):
            return port
    raise RuntimeError("没有可用的前端端口")


def _any_listening(port: int) -> bool:
    # Vite 在 Windows 上按 localhost 解析，常仅绑定 ::1；同时探测 IPv4 与 IPv6。
    for host in ("127.0.0.1", "::1"):
        probe = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        probe.settimeout(0.4)
        try:
            probe.connect((host, port))
            return True
        except OSError:
            pass
        finally:
            probe.close()
    return False


# 隔离前端端口必须在导入 app.interfaces.http 前设置：Host / Origin 白名单在模块导入时求值。
FRONTEND_PORT = _free_port(5175)
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"
os.environ["FIT_AGENT_FRONTEND_PORT"] = str(FRONTEND_PORT)

import asyncio
import json
import subprocess
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import uvicorn

from app.agent.agent_loop import run_agent_loop
from app.ai.stream import stream
from app.domain.business.models import (
    PlanContent,
    PlanDay,
    PlanExercise,
    PlanRecord,
    ProfileContent,
    WorkoutContent,
    WorkoutExercise,
    WorkoutRecord,
    WorkoutSet,
)
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces import http as interface

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
BACKEND_PORT = 8000
BROWSER_SESSION = "fit-plan-adjust-e2e"
BROWSER_PROFILE: "Path | None" = None
BROWSER_IO: "Path | None" = None
BROWSER_EXE = Path(os.environ["APPDATA"]) / "npm/node_modules/agent-browser/bin/agent-browser-win32-x64.exe"
MODEL_LIMIT = 48

MESSAGE = "textarea[aria-label='消息']"
SEND = "button[aria-label='发送']"
STOP = "button[aria-label='停止']"
SCROLLER = "[data-slot=message-scroller-content]"
TRACE = "document.querySelector('[data-slot=message-scroller-content]').innerText"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def business_today() -> date:
    return datetime.fromtimestamp(now_ms() / 1000, tz=timezone(timedelta(hours=8))).date()


class Observation:
    # 观测每个 run 的公开事件、真实模型请求与用量，并作为请求预算与凭据保护的执行点。
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runs: list[dict] = []
        self.completed = 0
        self.usage: list[dict] = []
        self.requests: list[dict] = []
        self.credential_hit = False
        self.limit_error: str | None = None

    def begin(self) -> int:
        with self.lock:
            self.runs.append({"events": []})
            return len(self.runs) - 1

    def record(self, index: int, event: dict) -> None:
        with self.lock:
            self.runs[index]["events"].append(
                {"type": event.get("type"), "payload": _jsonable(event)}
            )

    def finish(self) -> None:
        with self.lock:
            self.completed += 1

    def latest(self) -> dict:
        return self.runs[-1]


OBS = Observation()


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value
    return value


def install_observer(state: dict) -> None:
    # 观测器记录请求预算、usage、业务投射凭据检查；真实执行仍然通过生产 run_agent_loop。
    def observed_loop(prompts, context, config, emit, signal=None, stream_fn=stream):
        index = OBS.begin()

        def check_budget() -> None:
            total = len(state.get("requests", [])) + len(OBS.requests)
            if total >= MODEL_LIMIT:
                OBS.limit_error = "真实模型验收请求预算已耗尽"
                raise RuntimeError(OBS.limit_error)

        def observed_stream(model, llm_context, options):
            check_budget()
            OBS.requests.append({"index": len(OBS.requests) + 1, "at": now_ms()})
            for message in llm_context.get("messages", []):
                sections = getattr(message, "sections", None) or {}
                if "business_context" in sections:
                    projection = json.loads(sections["business_context"])
                    if config.contains_credentials(projection):
                        OBS.credential_hit = True
                        raise RuntimeError("验收上下文命中凭据保护")
            return stream(model, llm_context, options)

        async def observed_emit(event):
            if event.get("type") == "message_end":
                message = event.get("message")
                usage = getattr(message, "usage", None)
                if usage is not None:
                    OBS.usage.append(usage.model_dump())
            OBS.record(index, event)
            await emit(event)

        async def runner():
            try:
                return await run_agent_loop(
                    prompts, context, config, observed_emit, signal, stream_fn=observed_stream
                )
            finally:
                OBS.finish()

        return runner()

    interface.run_agent_loop = observed_loop


class Server:
    # 固定在 127.0.0.1:8000：Host/Origin 白名单与 Vite 代理目标都指向该端口。
    def __init__(self, app) -> None:
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", BACKEND_PORT))
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", log_level="warning", access_log=False)
        )
        self._thread = Thread(target=self._server.run, kwargs={"sockets": [self._listener]})

    def __enter__(self) -> "Server":
        self._thread.start()
        deadline = time.monotonic() + 15
        while (
            not self._server.started
            and self._thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not self._server.started:
            raise AssertionError("隔离后端未启动")
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(90)
        self._listener.close()
        if self._thread.is_alive():
            raise AssertionError("隔离后端未退出")

    def __exit__(self, *_error) -> None:
        self.stop()


def start_frontend(log_path: Path) -> subprocess.Popen:
    environment = dict(os.environ)
    environment["FIT_AGENT_FRONTEND_PORT"] = str(FRONTEND_PORT)
    log = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        ["node", "node_modules/vite/bin/vite.js"],
        cwd=str(FRONTEND),
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"前端进程提前退出：{log_path.read_text(encoding='utf-8')}")
        if _any_listening(FRONTEND_PORT):
            return process
        time.sleep(0.3)
    try:
        process.terminate()
    finally:
        raise AssertionError("前端端口未就绪")


def stop_frontend(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()


def browser(*args, timeout: int = 120) -> dict:
    # 输出重定向到文件：浏览器 daemon 与 Chrome 会继承子进程句柄，管道会阻塞读取。
    command = [str(BROWSER_EXE), "--session", BROWSER_SESSION]
    if BROWSER_PROFILE is not None:
        command += ["--profile", str(BROWSER_PROFILE)]
    command += ["--json", *[str(arg) for arg in args]]
    directory = BROWSER_IO if BROWSER_IO is not None else Path.cwd()
    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / "browser-command.json"
    err_path = directory / "browser-command.log"
    with open(out_path, "w", encoding="utf-8") as out, open(
        err_path, "w", encoding="utf-8"
    ) as err:
        result = subprocess.run(command, stdout=out, stderr=err, timeout=timeout)
    content = out_path.read_text(encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(
            f"浏览器命令失败 {args}: {err_path.read_text(encoding='utf-8') or content}"
        )
    reply = json.loads(content)
    if not reply.get("success"):
        raise AssertionError(f"浏览器命令未成功 {args}: {content}")
    return reply.get("data")


def js(expression: str):
    return browser("eval", expression).get("result")


def until(expression: str, timeout: int = 120, interval: float = 0.3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if js(expression):
            return
        time.sleep(interval)
    raise AssertionError(f"浏览器条件超时：{expression}")


def screenshot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    browser("screenshot", str(path))


def http_client(timeout: float = 300) -> httpx.Client:
    return httpx.Client(
        base_url=f"http://127.0.0.1:{BACKEND_PORT}",
        headers={"Host": f"127.0.0.1:{BACKEND_PORT}", "Origin": f"http://127.0.0.1:{FRONTEND_PORT}"},
        timeout=timeout,
        trust_env=False,
    )


def call(coro, timeout: float = 60):
    return asyncio.run_coroutine_threadsafe(coro, interface.app.state.loop).result(timeout=timeout)


def tool_calls(run: dict) -> list[dict]:
    starts = {}
    finals = {}
    results = {}
    for event in run["events"]:
        kind = event["type"]
        data = event["payload"]
        call_id = data.get("tool_call_id") or data.get("toolCallId")
        if kind == "tool_start":
            starts[call_id] = data
        elif kind == "tool_execution_end":
            finals[call_id] = data
        elif kind == "tool_result":
            results[call_id] = data
    calls = []
    for call_id, start in starts.items():
        final = finals.get(call_id, {})
        result = results.get(call_id, {})
        calls.append(
            {
                "call_id": call_id,
                "name": start.get("name"),
                "arguments": start.get("arguments"),
                "is_error": final.get("is_error"),
                "content": final.get("content"),
                "entry_id": result.get("entry_id") or result.get("message_id"),
                "parent_id": result.get("parent_id"),
            }
        )
    return calls


def named(run: dict, name: str) -> list[dict]:
    return [item for item in tool_calls(run) if item["name"] == name]


def parse_content(item: dict):
    return json.loads(item["content"])


def last_message_text(run: dict) -> str:
    for event in reversed(run["events"]):
        if event["type"] == "message_end":
            message = event["payload"]
            return "".join(
                block.get("text", "")
                for block in message.get("content", [])
                if block.get("type") == "text"
            )
    return ""


def wait_run(observation: Observation, before: int, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while observation.completed <= before and time.monotonic() < deadline:
        time.sleep(0.2)
    if observation.completed <= before:
        raise AssertionError("等待模型运行完成超时")
    until(f"!document.querySelector({json.dumps(STOP)})", timeout=120)
    until(f"document.querySelector({json.dumps(SCROLLER)}).getAttribute('aria-busy') !== 'true'", timeout=120)


def send_and_wait(observation: Observation, text: str, timeout: int = 300) -> dict:
    before = observation.completed
    browser("fill", MESSAGE, text)
    browser("press", "Enter")
    wait_run(observation, before, timeout)
    return observation.latest()


def send_attachment(observation: Observation, text: str, file_path: Path, timeout: int = 300) -> dict:
    before = observation.completed
    browser("upload", "input[type=file]", str(file_path))
    until("document.querySelectorAll('input[type=file]').length >= 1")
    browser("fill", MESSAGE, text)
    browser("press", "Enter")
    wait_run(observation, before, timeout)
    return observation.latest()


def open_session(session_id: str) -> None:
    browser("open", f"{FRONTEND_URL}/sessions/{session_id}")
    until("!!document.querySelector(\"textarea[aria-label='消息']\")", timeout=120)


def open_path(path: str) -> None:
    browser("open", f"{FRONTEND_URL}{path}")
    until("!!document.querySelector('main')", timeout=120)


def create_session(http: httpx.Client, title: str) -> str:
    session_id = str(uuid4())
    response = http.post("/api/sessions", json={"session_id": session_id, "title": title})
    assert response.status_code in {200, 201}, response.text
    return session_id


def current_plan(http: httpx.Client) -> dict:
    response = http.get("/api/plans/current")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    return response.json()


def list_plans(http: httpx.Client) -> list:
    response = http.get("/api/plans")
    assert response.status_code == 200
    return response.json()


def list_workouts(http: httpx.Client, query: str = "page_size=100") -> dict:
    response = http.get(f"/api/workouts?{query}")
    assert response.status_code == 200
    return response.json()


def database_rows(db_path: Path, sql: str, parameters: tuple = ()) -> list:
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql, parameters).fetchall()


def workload_records(today: date) -> list[WorkoutRecord]:
    plans = [
        ("0025", "杠铃卧推", "barbell_total", [(8, 60.0), (8, 62.5), (6, 65.0)]),
        ("1436", "杠铃高杠深蹲", "barbell_total", [(6, 80.0), (6, 85.0), (5, 90.0)]),
        ("0027", "杠铃俯身划船", "barbell_total", [(10, 45.0), (10, 47.5)]),
    ]
    records = []
    for offset in range(12):
        day = (today - timedelta(days=offset)).isoformat()
        exercise_id, name, convention, sets = plans[offset % len(plans)]
        records.append(
            WorkoutRecord(
                id=str(uuid4()),
                performed_on=day,
                version=1,
                content=WorkoutContent(
                    exercises=[
                        WorkoutExercise(
                            exercise_id=exercise_id,
                            name=name,
                            load_convention=convention,
                            sets=[
                                WorkoutSet(reps=reps, weight_kg=weight, duration_seconds=None)
                                for reps, weight in sets
                            ],
                        )
                    ],
                    notes=f"验收样例记录，训练日 {day}。",
                ),
                created_at=now_ms() - offset,
                updated_at=now_ms() - offset,
            )
        )
    return records


def baseline_plan() -> PlanRecord:
    return PlanRecord(
        id=str(uuid4()),
        is_current=True,
        created_at=now_ms(),
        content=PlanContent(
            repeat=True,
            days=[
                PlanDay(
                    kind="training",
                    focus="推（胸、肩、三头）",
                    exercises=[
                        PlanExercise(
                            exercise_id="0025", name="杠铃卧推", sets=4, reps=8,
                            duration_seconds=None, weight_kg=60.0, load_convention="barbell_total",
                            rest_seconds=150.0,
                        ),
                        PlanExercise(
                            exercise_id="0047", name="杠铃上斜卧推", sets=3, reps=10,
                            duration_seconds=None, weight_kg=40.0, load_convention="barbell_total",
                            rest_seconds=120.0,
                        ),
                    ],
                    notes="用户提供的现状：胸部训练日。",
                ),
                PlanDay(
                    kind="training",
                    focus="拉（背、二头）",
                    exercises=[
                        PlanExercise(
                            exercise_id="0027", name="杠铃俯身划船", sets=4, reps=8,
                            duration_seconds=None, weight_kg=50.0, load_convention="barbell_total",
                            rest_seconds=120.0,
                        ),
                        PlanExercise(
                            exercise_id="0007", name="交替侧向下拉", sets=3, reps=12,
                            duration_seconds=None, weight_kg=None, load_convention="machine_display",
                            rest_seconds=90.0,
                        ),
                    ],
                    notes=None,
                ),
                PlanDay(
                    kind="training",
                    focus="腿",
                    exercises=[
                        PlanExercise(
                            exercise_id="1436", name="杠铃高杠深蹲", sets=4, reps=6,
                            duration_seconds=None, weight_kg=80.0, load_convention="barbell_total",
                            rest_seconds=180.0,
                        ),
                    ],
                    notes="用户提供的现状：腿部训练日，其余动作参数未知。",
                ),
                PlanDay(kind="rest", focus=None, exercises=[], notes=None),
            ],
            notes="用户提供的推、拉、腿、休四日循环。部分动作参数未知。",
            suggested_fields=["/days/1/exercises/0/rest_seconds", "/days/2/exercises/0/reps"],
        ),
    )


def baseline_profile() -> ProfileContent:
    return ProfileContent(
        goal="增肌与一般力量提升",
        experience="力量训练新手，系统训练约三个月",
        environment="普通商业健身房",
        availability="每周三次，每次约四十五分钟",
        health_notes="无伤病或不适",
        movement_restrictions=None,
        unavailable_equipment=[],
        forbidden_exercise_ids=[],
    )


async def _seed() -> dict:
    repository = interface.app.state.business._repository
    today = business_today()
    plan = baseline_plan()
    workouts = workload_records(today)
    async with repository.transaction():
        await repository.save_profile(baseline_profile(), 1, now_ms())
        await repository.insert_plan(plan)
        for record in workouts:
            await repository.insert_workout(record)
    return {"plan_id": plan.id, "workout_ids": [record.id for record in workouts], "business_date": today.isoformat()}


def seed_baseline(http: httpx.Client) -> dict:
    existing = http.get("/api/profile").json()
    if existing["version"] is not None:
        return {}
    seeded = call(_seed())
    return seeded


def write_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
