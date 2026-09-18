"""Stage 5：§3.7 HTTP／错误边界与 §3.8 SSE 事件契约的代码前断言（子任务 01），以及 Agent 三端点
的 §7.4–§7.6 行为测试（子任务 05）。

依据：``refactor-log/stage5.md`` §3.4／§3.7／§3.8／§6 Subtask 01／§6 Subtask 05／§7.4–§7.6；
``LANGGRAPH_REFACTOR_PLAN.md`` §9.4；``Fit-Agent-LangGraph-重构讨论总结.md`` §9／§10。

上半部分（子任务 01）只冻结契约：三个端点的请求／响应形状、流前 JSON 与流内 SSE 错误边界、五类产品
事件与禁止项。已合入源码侧的交叉断言只读 Stage 5 要复用的既有事实：统一 JSON 错误形状与错误码
（``api/dto.py``）、``conversation_id`` 即 ``thread_id``（``graph/checkpointer.py``）、页面恢复用的
既有 ``GET /api/plans``（``api/routes_plans.py``）与三个模型环境变量名（``config.py``）。

下半部分（子任务 05）用真实 ``create_app`` ＋ 真实 lifespan ＋ 固定替身模型驱动三个端点：事件的
事件名集合与载荷键逐项等于 §3.8 契约（``waiting`` 只含 ``draft_plan_id``）、成功路径的 draft 落库与
``done`` 数据、同类 reuse／regenerate 的 id／version／source 与失败不改原 draft、跨类型与普通 adjust 的
明确失败、安全命中先于 Router 与已有 draft 的复用／冲突／regenerate、active 与 draft 读取（§3.5；A1：命中时 ``done.intent`` 为 ``null``）、流前 JSON 与流内单条
``error`` 的边界、响应与存档字节不含密钥／端点／模型名、确认／拒绝与领域幂等结果一致，以及客户端断开
不触发任何写入也不回滚已持久化 draft。

全部行为不调真实模型、不需要任何 ``MODEL_*`` 环境变量（生产装配的模型入口是惰性的，见
``api/app.py::_lazy_model_call``）。
"""

import asyncio
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import app as api_app
from api import dto, routes_plans
from api.app import create_app
from api.deps import current_business_date
from api.routes_agent import (
    AGENT_RUN_ERROR_MESSAGE,
    MODEL_CONFIGURATION_ERROR_MESSAGE,
    AgentRuntime,
)
from api.routes_agent import _sse_frames as route_sse_frames
from config import (
    MODEL_API_KEY_ENV,
    MODEL_BASE_URL_ENV,
    MODEL_MODEL_ENV,
    checkpoint_database_path,
)
from domain.actions.service import ActionCatalogService
from domain.plans.schema import Plan
from domain.plans.service import (
    PlanActivationService,
    PlanPersistenceService,
    PlanReadService,
)
from domain.profile.schema import Fact, Profile
from domain.profile.service import ProfileService
from domain.stats.repo import StatsRepo
from domain.stats.service import StatsService
from graph.checkpointer import open_checkpointer, thread_config
from graph.context import MemoryAssembler
from graph.model import (
    MODEL_CALL_FAILED_MESSAGE,
)
from graph.nodes import (
    ADJUSTMENT_PLANNER_SYSTEM_PROMPT,
    EVALUATOR_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    GeneratePlanDeps,
    GeneratePlanRun,
    ModelRequestBudget,
)
from graph.router import ROUTER_SYSTEM_PROMPT
from graph.skills import SkillLoader
from graph.workflow import (
    NODE_NAMES,
    REJECT_DRAFT_MESSAGE,
    SAFETY_STOP_MESSAGE,
    AgentEvent,
    AgentRunDeps,
    build_generate_plan_graph,
    stream_agent_run,
)
from storage.db import Database
from tests.conftest import Stage5Plan

#: §3.7 三个端点的请求与响应形状（代码块原样冻结）。
AGENT_ENDPOINT_LINES = (
    "POST /api/agent/run",
    "  body: { conversation_id: string, request: string, regenerate?: boolean }",
    "  → text/event-stream（五类产品事件）",
    "POST /api/agent/confirm",
    "  body: { conversation_id: string, plan_id: number }",
    "  → 200 { plan: PlanWire }（激活成功或幂等已 active）",
    "POST /api/agent/reject",
    "  body: { conversation_id: string, plan_id: number }",
    "  → 200 { plan: PlanWire }（draft 已 archived）",
)

#: §3.7 六条正文：UUID 校验、regenerate 同类替换、流前 JSON、流内 SSE error、confirm/reject 错误、秘密不回流。
AGENT_ERROR_BOUNDARY_LINES = (
    "- `conversation_id`：请求体按 UUID 校验；前端每次新一轮生成/调整生成一个值，确认/拒绝必须沿用同一值。",
    "- `regenerate` 按 §3.4 的同类替换规则处理；缺省/false 且已有 draft 时不调模型。",
    "- 请求 JSON 形状、字段类型或 UUID 非法：流建立前返回既有 JSON 错误形状。",
    "- `/api/agent/run` 流建立后的模型配置、超时、Router、无 active、draft 冲突等运行错误：发送一个 SSE `error` 后关闭，不再混用 JSON。",
    "- `/api/agent/confirm`／`reject` 的不存在、ID 不匹配、状态冲突、日期过期和再校验失败：使用既有 JSON 错误形状与明确 HTTP 状态。",
    "- 任何错误均不回显 API Key、Base URL、模型名、SQL、文件路径或堆栈。",
)

#: §3.8 五类产品事件整表（event，data，含义）；事件集合是封闭的五种。
SSE_EVENT_TABLE = (
    ("`node`", '`{ "name": string }`', "当前 Graph 节点/阶段名"),
    ("`message`", '`{ "text": string }`', "面向用户的可见文本（安全提示、引导、简要结果）"),
    ("`waiting`", '`{ "draft_plan_id": number }`', "已持久化 draft，等待确认"),
    (
        "`done`",
        '`{ "ok": true, "intent": string, "termination_reason": string \\| null, "draft_plan_id": number \\| null }`',
        "Run 正常结束",
    ),
    ("`error`", '`{ "message": string }`', "运行错误（无密钥、无原始模型事件）"),
)

#: §3.8 收尾：不发隐藏推理／系统提示词／Provider 配置／原始事件；断线不是提交信号；恢复用既有只读端点。
SSE_PROHIBITION_RULE = (
    "禁止：隐藏推理、完整系统提示词、Provider 配置、原始 LangChain 事件。SSE 断线本身不触发额外业务写入，"
    "也不回滚已完成的 draft 持久化；它不是取消、确认或拒绝信号。页面恢复时用既有 `GET /api/plans` "
    "定位唯一 draft，不新增 checkpoint 查询端点。"
)


def test_agent_http_contract_lines_are_frozen(stage5_plan: Stage5Plan) -> None:
    """§3.7：三个端点的方法、路径、请求体字段与响应形状整段冻结（含 ``regenerate`` 可选标志）。"""
    section = stage5_plan.section("3.7 HTTP 与错误契约")

    assert [line for line in AGENT_ENDPOINT_LINES if line not in section] == []
    assert [line for line in AGENT_ERROR_BOUNDARY_LINES if line not in section] == []


def test_conversation_id_is_the_uuid_thread_id(stage5_plan: Stage5Plan) -> None:
    """§3.7：``conversation_id`` 按 UUID 校验，且就是 Checkpointer 的 ``thread_id``（不另建映射）。"""
    section = stage5_plan.section("3.7 HTTP 与错误契约")

    assert AGENT_ERROR_BOUNDARY_LINES[0] in section
    assert "conversation_id: string" in section
    assert thread_config("stage5-agent-run") == {"configurable": {"thread_id": "stage5-agent-run"}}


def test_agent_regenerate_follows_the_same_kind_replacement_rule(stage5_plan: Stage5Plan) -> None:
    """§3.7：``regenerate`` 按 §3.4 同类替换处理；缺省/false 且已有 draft 时不调模型。"""
    assert AGENT_ERROR_BOUNDARY_LINES[1] in stage5_plan.section("3.7 HTTP 与错误契约")


def test_agent_error_boundary_is_json_before_the_stream_and_sse_inside_it(
    stage5_plan: Stage5Plan,
) -> None:
    """§3.7：请求形状／类型／UUID 错误在流建立前返回既有 JSON 错误形状；流内运行错误只发一个 SSE
    ``error`` 后关闭，confirm／reject 错误也复用同一 JSON 形状与明确 HTTP 状态。"""
    section = stage5_plan.section("3.7 HTTP 与错误契约")

    assert AGENT_ERROR_BOUNDARY_LINES[2] in section
    assert AGENT_ERROR_BOUNDARY_LINES[3] in section
    assert AGENT_ERROR_BOUNDARY_LINES[4] in section
    # 既有 JSON 错误形状的唯一出处：统一错误码 + 统一异常处理器（不在 Agent 层另造形状）。
    assert dto.ERROR_CODE_INVALID_REQUEST == "invalid_request"
    assert callable(dto.install_error_handlers)


def test_agent_errors_never_echo_secrets_or_provider_configuration(stage5_plan: Stage5Plan) -> None:
    """§3.7／§3.8：任何错误都不回显 API Key、Base URL、模型名、SQL、文件路径或堆栈。"""
    assert AGENT_ERROR_BOUNDARY_LINES[5] in stage5_plan.section("3.7 HTTP 与错误契约")
    assert SSE_EVENT_TABLE[4][2] == "运行错误（无密钥、无原始模型事件）"
    assert (MODEL_API_KEY_ENV, MODEL_BASE_URL_ENV, MODEL_MODEL_ENV) == (
        "MODEL_API_KEY",
        "MODEL_BASE_URL",
        "MODEL_MODEL",
    )


def test_sse_event_contract_is_exactly_five_product_events(stage5_plan: Stage5Plan) -> None:
    """§3.8 五类事件整表冻结：``node``／``message``／``waiting``／``done``／``error`` 的载荷键不得增减。"""
    assert stage5_plan.table("3.8 SSE 事件契约", "data（JSON）") == SSE_EVENT_TABLE


def test_sse_forbids_raw_events_and_disconnect_is_not_a_signal(stage5_plan: Stage5Plan) -> None:
    """§3.8：不发隐藏推理／系统提示词／Provider 配置／原始事件；断线不触发写入，恢复用既有 ``GET /api/plans``。"""
    assert SSE_PROHIBITION_RULE in stage5_plan.section("3.8 SSE 事件契约")
    assert "/api/plans" in {route.path for route in routes_plans.router.routes}


# ---------- §7.4–§7.6 行为测试：真实三端点 ＋ 固定替身模型（无 API Key） ----------

#: 本次 Run 注入的业务日（确认／激活的日期新鲜度都按它判定）。
BUSINESS_DAY = date(2026, 6, 1)
SCHEDULED_ON = "2026-06-02"
FIXED_NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
CREATED_AT = "2026-06-01T08:00:00+00:00"
CONVERSATION_ID = "2f7f2b6e-6d2a-4b3c-9e1d-0a5c8f4b7d21"
OTHER_CONVERSATION_ID = "5c1d8e3a-9b47-4f02-8a6c-1d9e7b3f5a08"
#: 封闭词表命中的请求：``生成计划``→``generate_plan``、``调整计划``→``adjust_plan``（不调分类模型）。
REQUEST = "生成计划"
ADJUST_REQUEST = "调整计划"
#: 急性关键词（``domain/profile/safety.py`` 的封闭词表原词）：只作安全终止，不生成计划。
RED_FLAG_REQUEST = "生成计划，但最近晕厥"
#: 调整请求的安全命中版本：``调整计划`` 同样是封闭词表命中（分类不调模型），且带同一份急性关键词。
RED_FLAG_ADJUST_REQUEST = "调整计划，但最近晕厥"
PULL_UP = "pull-up"
WEEKLY_FREQUENCY = 1

#: §3.8 五类事件的 ``data`` 键集合：与冻结的契约表逐项相等，事件名集合也是封闭的五种。
SSE_EVENT_KEYS: dict[str, frozenset[str]] = {
    "node": frozenset({"name"}),
    "message": frozenset({"text"}),
    "waiting": frozenset({"draft_plan_id"}),
    "done": frozenset({"ok", "intent", "termination_reason", "draft_plan_id"}),
    "error": frozenset({"message"}),
}

#: 生成计划成功路径的节点事件序列（``wait_for_confirmation`` 停在 interrupt 上，不出节点事件）。
GENERATE_NODE_SEQUENCE = (
    "safety_check",
    "validate_required_profile",
    "load_context",
    "load_skill",
    "planner",
    "evaluator",
    "persist_draft",
)

#: 调整计划成功路径的节点事件序列（调整分支在画像前提前先预读当前 active）。
ADJUST_NODE_SEQUENCE = (
    "safety_check",
    "require_active_plan",
    "validate_required_profile",
    "load_context",
    "load_skill",
    "planner",
    "evaluator",
    "persist_draft",
)


class ScriptedModel:
    """按系统提示词分派固定响应的模型替身；未脚本化的提示词即失败（读环境变量即失败）。"""

    def __init__(self, scripts: Mapping[str, Sequence[str]] | None = None) -> None:
        self._scripts = {
            prompt: list(items) for prompt, items in (scripts or {}).items()
        }
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system_prompt: str, user_payload: str) -> str:
        self.calls.append((system_prompt, user_payload))
        script = self._scripts.get(system_prompt)
        if script is None:
            raise AssertionError(
                f"固定替身模型收到未脚本化的系统提示词：{system_prompt[:40]!r}"
            )
        if not script:
            raise AssertionError("固定替身模型没有更多脚本响应")
        return script.pop(0)

    @property
    def prompts(self) -> list[str]:
        """本次 Run 的系统提示词调用序列（即「哪几个 Agent 被调用」，不落提示词全文）。"""
        return [prompt for prompt, _ in self.calls]


class FailingModel:
    """Provider 类异常替身：抛出的文本里带密钥／端点／模型名（生产模型入口必须换成固定文本）。"""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def __call__(self, system_prompt: str, user_payload: str) -> str:
        self.calls += 1
        raise self._error


#: 超时用例注入的 Run 时限（秒）：只调小本次 Run 的预算，冻结的 60／180／5 上限不动（§3.9 不新增配置）。
TIMEOUT_RUN_SECONDS = 1.0
#: 超时用例里模型替身的阻塞时长（秒）：远大于 Run 时限，保证是被时限取消而不是自己返回。
TIMEOUT_BLOCK_SECONDS = 20.0


class BlockingModel:
    """超时用例的模型替身：一直阻塞到 Run 时限取消本次调用。

    若它真能返回（时限没生效），就抛出带密钥／端点的原文——该文本会进错误事件，被下面的断言抓到。
    """

    def __init__(self, leak: str) -> None:
        self._leak = leak
        self.calls = 0

    async def __call__(self, system_prompt: str, user_payload: str) -> str:
        self.calls += 1
        await asyncio.sleep(TIMEOUT_BLOCK_SECONDS)
        raise AssertionError(self._leak)


def _profile() -> Profile:
    """固定画像：每周训练次数是明确值（Planner 前前提与激活再校验都要它）。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=Fact.known(WEEKLY_FREQUENCY),
        available_equipment=Fact.known(("barbell", "bodyweight")),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.denied(),
        forbidden_exercise_ids=Fact.denied(),
    )


def _plan_text(*, explanation: str = "初次生成") -> str:
    """固定替身 Planner 的响应：一个结构合法的单训练日计划（每周 1 练，自重动作不带负荷）。"""
    return json.dumps(
        {
            "goal": "增肌",
            "starts_on": BUSINESS_DAY.isoformat(),
            "explanation": explanation,
            "weekly_frequency": WEEKLY_FREQUENCY,
            "training_days": [
                {
                    "scheduled_on": SCHEDULED_ON,
                    "exercises": [
                        {
                            "exercise_id": PULL_UP,
                            "sets": 3,
                            "prescription": {
                                "type": "bodyweight_reps",
                                "reps_min": 8,
                                "reps_max": 12,
                                "progression_note": None,
                            },
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


def _rubric_text(*, goal_alignment: bool = True) -> str:
    """固定替身 Evaluator 的三个布尔判定；``goal_alignment=False`` 是硬门槛阻断。"""

    def verdict(passed: bool) -> dict[str, Any]:
        return {"passed": passed, "reason": "固定替身判定"}

    return json.dumps(
        {
            "goal_alignment": verdict(goal_alignment),
            "schedule_reasonableness": verdict(True),
            "explanation_quality": verdict(True),
        },
        ensure_ascii=False,
    )


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _insert_plan(
    db: Database,
    *,
    version: int,
    status: str,
    content: str,
    confirmed_at: str | None = None,
    source_plan_id: int | None = None,
) -> int:
    """直接 SQL 预置一个计划版本行（正式写入入口是被测端点），返回计划身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, source_plan_id, structured_content,"
            " created_at, confirmed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (version, status, source_plan_id, content, CREATED_AT, confirmed_at),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _plan_rows(db: Database) -> list[dict[str, Any]]:
    """``plans`` 全列快照（按版本升序）：写入／未写入的证据。"""

    async def op(conn: Any) -> list[dict[str, Any]]:
        async with conn.execute(
            "SELECT id, version, status, source_plan_id, structured_content,"
            " evaluator_result, created_at, confirmed_at, archived_at"
            " FROM plans ORDER BY version"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


def _scripted_deps(db: Database, model: Any) -> GeneratePlanDeps:
    """计划子图依赖（固定替身模型 ＋ 注入时钟）：与 ``api/app.py`` 的生产装配同形，只换模型与时钟。"""
    return GeneratePlanDeps(
        profiles=ProfileService(db),
        catalog=ActionCatalogService(db),
        stats=StatsRepo(db),
        assembler=MemoryAssembler(db),
        skills=SkillLoader(),
        persistence=PlanPersistenceService(db),
        plans=PlanReadService(db),
        activation=PlanActivationService(db),
        model=model,
        now=lambda: FIXED_NOW,
    )


def _scripted_runtime(db: Database, checkpointer: Any, model: Any) -> AgentRuntime:
    """把固定替身装进 Agent 三端点的装配：唯一模型入口 ＋ 同一份计划／激活服务。"""
    deps = _scripted_deps(db, model)
    return AgentRuntime(
        graph=build_generate_plan_graph(deps, checkpointer=checkpointer),
        deps=deps,
        run_deps=AgentRunDeps(
            model=model,
            stats=StatsService(db),
            plans=deps.plans,
            persistence=deps.persistence,
        ),
    )


def _parse_frames(text: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE 文本 → ``(事件名, data)`` 列表，并逐帧校验 §3.8 的事件名与载荷键集合。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        assert len(lines) == 2, f"SSE 帧必须恰为 event ＋ data 两行：{block!r}"
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        name = lines[0][len("event: ") :]
        assert name in SSE_EVENT_KEYS, f"SSE 事件名不在五类产品事件里：{name}"
        data = json.loads(lines[1][len("data: ") :])
        assert set(data) == SSE_EVENT_KEYS[name], f"{name} 的 data 键不符：{sorted(data)}"
        frames.append((name, data))
    return frames


def _sse_frames(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """SSE 响应 → ``(事件名, data)`` 列表（HTTP 报文形态 ＋ 逐帧契约校验）。"""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return _parse_frames(response.text)


def _event_names(frames: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in frames]


def _content(row: dict[str, Any]) -> dict[str, Any]:
    """计划行的 ``structured_content`` 解码（直接 SQL 读到的是 TEXT 列）。"""
    return json.loads(row["structured_content"])


def _node_names(frames: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    """``node`` 事件里的阶段／节点名序列（节点事件的唯一数据键）。"""
    return [data["name"] for name, data in frames if name == "node"]


@contextmanager
def _app_client(tmp_path: Path) -> Iterator[TestClient]:
    """真实 app（临时数据目录 ＋ 真实 lifespan）＋ 固定业务日期；启动不读任何 ``MODEL_*``。"""
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "dist")
    app.dependency_overrides[current_business_date] = lambda: BUSINESS_DAY
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


@dataclass
class _AgentApp:
    """一次测试的 app 客户端、固定替身模型与业务库（预置事实用直接 SQL）。"""

    client: TestClient
    model: Any
    db: Database

    def run(
        self,
        request: str,
        *,
        conversation_id: str = CONVERSATION_ID,
        regenerate: bool = False,
    ) -> list[tuple[str, dict[str, Any]]]:
        return _sse_frames(
            self.client.post(
                "/api/agent/run",
                json={
                    "conversation_id": conversation_id,
                    "request": request,
                    "regenerate": regenerate,
                },
            )
        )

    def plans(self) -> list[dict[str, Any]]:
        return self.client.portal.call(_plan_rows, self.db)

    def seed_profile(self) -> None:
        self.client.portal.call(partial(ProfileService(self.db).update, _profile()))

    def seed_plan(
        self,
        *,
        version: int,
        status: str,
        content: str,
        confirmed_at: str | None = None,
        source_plan_id: int | None = None,
    ) -> int:
        return self.client.portal.call(
            partial(
                _insert_plan,
                version=version,
                status=status,
                content=content,
                confirmed_at=confirmed_at,
                source_plan_id=source_plan_id,
            ),
            self.db,
        )


@contextmanager
def _scripted_app(
    tmp_path: Path, *, scripts: Mapping[str, Sequence[str]] | None = None
) -> Iterator[_AgentApp]:
    """真实 app ＋ 固定替身模型：整条链路不读环境变量、不调真实模型（无 API Key 可测）。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        model = ScriptedModel(scripts)
        client.app.state.agent_runtime = _scripted_runtime(
            db, client.app.state.checkpointer, model
        )
        yield _AgentApp(client=client, model=model, db=db)


# ---------- §4.4／§4.3 生产装配 ----------


def test_production_runtime_assembles_the_plan_subgraph_and_one_model_entry(
    tmp_path: Path,
) -> None:
    """§4.3／§4.4：lifespan 装配一份 Planner／Evaluator 子图与唯一模型入口，不需要模型环境变量。"""
    with _app_client(tmp_path) as client:
        runtime: AgentRuntime = client.app.state.agent_runtime

        assert set(runtime.graph.get_graph().nodes) == set(NODE_NAMES) | {
            "__start__",
            "__end__",
        }
        # 唯一模型入口：计划链路与 Router／非计划分支共用同一个 callable。
        assert runtime.run_deps.model is runtime.deps.model
        # 运行入口与确认入口共用同一份只读计划服务、draft 读写服务与同一个领域激活服务。
        assert runtime.run_deps.plans is runtime.deps.plans
        assert runtime.run_deps.persistence is runtime.deps.persistence
        assert isinstance(runtime.deps.activation, PlanActivationService)


# ---------- §7.6 SSE／HTTP ----------


def test_run_stream_emits_exactly_the_frozen_five_product_events(tmp_path: Path) -> None:
    """§7.6：事件仅五种；``waiting`` 只含 ``draft_plan_id``；成功路径的 draft 落库且 ``done`` 与之一致。"""
    scripts = {
        PLANNER_SYSTEM_PROMPT: [_plan_text()],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()

        frames = agent.run(REQUEST)

        assert _event_names(frames) == [
            *["node"] * len(GENERATE_NODE_SEQUENCE),
            "waiting",
            "done",
        ]
        assert _node_names(frames) == list(GENERATE_NODE_SEQUENCE)
        draft_id = next(data["draft_plan_id"] for name, data in frames if name == "waiting")
        assert [data for name, data in frames if name == "done"] == [
            {
                "ok": True,
                "intent": "generate_plan",
                "termination_reason": None,
                "draft_plan_id": draft_id,
            }
        ]
        rows = agent.plans()
        assert len(rows) == 1
        assert rows[0]["id"] == draft_id
        assert rows[0]["status"] == "draft"
        assert rows[0]["source_plan_id"] is None
        assert _content(rows[0]) == json.loads(_plan_text())
        assert agent.model.prompts == [PLANNER_SYSTEM_PROMPT, EVALUATOR_SYSTEM_PROMPT]


def test_run_safety_stop_sends_one_message_and_writes_no_plan(tmp_path: Path) -> None:
    """§3.6／§3.8／A1：安全词命中先于 Router——不分类、不装配、不调模型，只发安全提示，不写任何计划行。

    ``done.intent`` 为 ``null``：本次 Run 没有 Router 结论（修复前是确定性命中的 ``generate_plan``）。
    """
    with _scripted_app(tmp_path) as agent:
        agent.seed_profile()

        frames = agent.run(RED_FLAG_REQUEST)

        assert _event_names(frames) == ["node", "node", "message", "done"]
        assert _node_names(frames) == ["safety_check", "safety_stop"]
        assert frames[-2][1] == {"text": SAFETY_STOP_MESSAGE}
        assert frames[-1][1] == {
            "ok": True,
            "intent": None,
            "termination_reason": "safety_stop",
            "draft_plan_id": None,
        }
        assert agent.model.calls == []
        assert agent.plans() == []


def test_run_red_flag_precedes_reusing_an_existing_generate_draft(tmp_path: Path) -> None:
    """§3.5／§3.4 第 5–6 条／A1：安全命中先于「已有生成 draft 的复用」与「同类 regenerate 替换」。

    普通请求（``regenerate`` 缺省）在修复前直接复用既有 draft 并发 ``waiting``，安全词根本不参与；
    ``regenerate=true`` 在修复前已经由子图的入口节点 ``safety_check`` 终止，这里一并断言显式重新生成
    不得绕过安全。两条路的可见结果都只有安全提示，原 draft 逐字段不变；A1 之后预检先于 Router，
    ``done.intent`` 为 ``null``（修复前是 ``generate_plan``）。
    """
    for regenerate in (False, True):
        with _scripted_app(tmp_path / f"red-flag-generate-{regenerate}") as agent:
            agent.seed_profile()
            agent.seed_plan(
                version=1, status="draft", content=_plan_text(explanation="既有草案")
            )
            before = agent.plans()

            frames = agent.run(RED_FLAG_REQUEST, regenerate=regenerate)

            assert _event_names(frames) == ["node", "node", "message", "done"], regenerate
            assert _node_names(frames) == ["safety_check", "safety_stop"], regenerate
            assert frames[-2][1] == {"text": SAFETY_STOP_MESSAGE}, regenerate
            assert frames[-1][1] == {
                "ok": True,
                "intent": None,
                "termination_reason": "safety_stop",
                "draft_plan_id": None,
            }, regenerate
            assert agent.model.calls == [], regenerate
            assert agent.plans() == before, regenerate


def test_run_red_flag_precedes_existing_draft_conflicts_and_the_active_read(
    tmp_path: Path,
) -> None:
    """§3.5／§3.4 第 5–6 条／A1：安全命中先于 draft 冲突、同类 regenerate 与 adjust 的 active 预读。

    修复前这些组合得到的都不是安全终止：普通 adjust 遇既有调整 draft 直接报「不覆盖既有 draft」冲突；
    来源已不是当前 active 的 regenerate 会先读一次 active 再冲突；跨类型（既有生成 draft ＋ 调整请求）
    报跨类型冲突。命中急性关键词后四种组合都只给安全提示、不读 active、不写任何行（来源仍是当前
    active 的 regenerate 在修复前也由子图入口终止，这里作为回归保留）；A1 之后预检先于 Router，
    ``done.intent`` 为 ``null``（修复前是确定性命中的 ``adjust_plan``）。
    """
    cases = (
        ("plain-adjust", False, "active"),
        ("same-source-regenerate", True, "active"),
        ("moved-source-regenerate", True, "archived"),
        ("cross-kind-generate-draft", False, None),
    )
    for name, regenerate, draft_source in cases:
        with _scripted_app(tmp_path / name) as agent:
            agent.seed_profile()
            active_id = agent.seed_plan(
                version=1,
                status="active",
                content=_plan_text(explanation="当前 active"),
                confirmed_at=CREATED_AT,
            )
            source_plan_id = None
            if draft_source == "active":
                source_plan_id = active_id
            elif draft_source == "archived":
                source_plan_id = agent.seed_plan(
                    version=2,
                    status="archived",
                    content=_plan_text(explanation="上一版 active"),
                )
            agent.seed_plan(
                version=3 if draft_source == "archived" else 2,
                status="draft",
                content=_plan_text(explanation="既有草案"),
                source_plan_id=source_plan_id,
            )
            before = agent.plans()

            frames = agent.run(RED_FLAG_ADJUST_REQUEST, regenerate=regenerate)

            assert _event_names(frames) == ["node", "node", "message", "done"], name
            assert _node_names(frames) == ["safety_check", "safety_stop"], name
            assert frames[-2][1] == {"text": SAFETY_STOP_MESSAGE}, name
            assert frames[-1][1] == {
                "ok": True,
                "intent": None,
                "termination_reason": "safety_stop",
                "draft_plan_id": None,
            }, name
            assert agent.model.calls == [], name
            assert agent.plans() == before, name


#: A1 裁决：三类非计划分支的确定性命中请求 ＋ 同一个急性关键词（封闭词表对每个 intent 都先判定）。
RED_FLAG_NON_PLAN_REQUESTS = (
    "打开打卡表单，最近晕厥",
    "记录一下，练了 5kg，最近晕厥",
    "查看进步，最近晕厥",
)

#: 零命中／多命中请求（Router 规则不覆盖，正常路径恰调一次分类模型）＋ 同一个急性关键词。
RED_FLAG_UNDETERMINED_REQUESTS = (
    "最近晕厥，不知道该怎么办",
    "生成计划并调整计划，最近晕厥",
)


def _intent_text(intent: str) -> str:
    """固定替身 Router 的响应：严格枚举形状 ``{ "intent": <五类之一> }``。"""
    return json.dumps({"intent": intent}, ensure_ascii=False)


def test_run_red_flag_precedes_every_non_plan_branch(tmp_path: Path) -> None:
    """A1／§3.5：三类非计划分支同样先过封闭词表——不发分支引导文本、零模型调用、零业务写入。

    修复前这三个请求分别确定为 ``form_record``／``natural_language_record``／``view_progress``：前两者直接
    发引导文本，``view_progress`` 还要调一次模型解释统计（未脚本化即失败）。命中急性关键词后三者都只发
    安全提示与 ``done.intent = null``，替身模型一次都不被调用。
    """
    for index, request in enumerate(RED_FLAG_NON_PLAN_REQUESTS):
        with _scripted_app(tmp_path / f"red-flag-non-plan-{index}") as agent:
            agent.seed_profile()

            frames = agent.run(request)

            assert _event_names(frames) == ["node", "node", "message", "done"], request
            assert _node_names(frames) == ["safety_check", "safety_stop"], request
            assert frames[-2][1] == {"text": SAFETY_STOP_MESSAGE}, request
            assert frames[-1][1] == {
                "ok": True,
                "intent": None,
                "termination_reason": "safety_stop",
                "draft_plan_id": None,
            }, request
            assert agent.model.calls == [], request
            assert agent.plans() == [], request


def test_run_red_flag_never_calls_the_router_fallback_model(tmp_path: Path) -> None:
    """A1／§3.5：零命中／多命中（正常路径恰调一次分类模型）时也先判定封闭词表——模型调用序列为空。

    替身给 Router 备好了脚本；``model.calls`` 为空即证明短路发生在 ``classify_intent`` 之前，而不是先分类
    再覆盖结论（修复前每个请求都会消费一次脚本，``done.intent`` 也是分类结论）。
    """
    scripts = {ROUTER_SYSTEM_PROMPT: [_intent_text("generate_plan")]}
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()

        for request in RED_FLAG_UNDETERMINED_REQUESTS:
            frames = agent.run(request)

            assert _event_names(frames) == ["node", "node", "message", "done"], request
            assert _node_names(frames) == ["safety_check", "safety_stop"], request
            assert frames[-2][1] == {"text": SAFETY_STOP_MESSAGE}, request
            assert frames[-1][1] == {
                "ok": True,
                "intent": None,
                "termination_reason": "safety_stop",
                "draft_plan_id": None,
            }, request

        assert agent.model.calls == []
        assert agent.plans() == []


def test_run_reuses_a_same_kind_draft_without_a_model_call(tmp_path: Path) -> None:
    """§3.4 第 5 条：已有同类 draft 且 ``regenerate`` 缺省时直接复用，不调模型、不写第二条 draft。"""
    scripts = {
        PLANNER_SYSTEM_PROMPT: [_plan_text()],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()
        draft_id = agent.seed_plan(
            version=1, status="draft", content=_plan_text(explanation="既有草案")
        )
        before = agent.plans()

        frames = agent.run(REQUEST)

        assert _event_names(frames) == ["waiting", "done"]
        assert frames[0][1] == {"draft_plan_id": draft_id}
        assert frames[1][1]["draft_plan_id"] == draft_id
        assert agent.model.calls == []
        assert agent.plans() == before


def test_run_regenerate_replaces_the_same_id_version_and_source(tmp_path: Path) -> None:
    """§7.6：同类 regenerate 替换同 id／version／source；生成 draft 的来源仍为 NULL。"""
    scripts = {
        PLANNER_SYSTEM_PROMPT: [_plan_text(explanation="重新生成")],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()
        draft_id = agent.seed_plan(
            version=1, status="draft", content=_plan_text(explanation="既有草案")
        )

        frames = agent.run(REQUEST, regenerate=True)

        assert _event_names(frames) == [
            *["node"] * len(GENERATE_NODE_SEQUENCE),
            "waiting",
            "done",
        ]
        assert _node_names(frames) == list(GENERATE_NODE_SEQUENCE)
        assert frames[-2][1] == {"draft_plan_id": draft_id}
        rows = agent.plans()
        assert len(rows) == 1
        assert rows[0]["id"] == draft_id
        assert rows[0]["version"] == 1
        assert rows[0]["status"] == "draft"
        assert rows[0]["source_plan_id"] is None
        assert _content(rows[0])["explanation"] == "重新生成"


def test_adjust_regenerate_replaces_the_same_adjust_draft_and_keeps_its_source(
    tmp_path: Path,
) -> None:
    """§7.5／§7.6：adjust 的同类 regenerate 只在来源仍是当前 active 时替换同一 id／version 并保持来源。"""
    scripts = {
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [_plan_text(explanation="调整后")],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()
        active_id = agent.seed_plan(
            version=1,
            status="active",
            content=_plan_text(explanation="当前 active"),
            confirmed_at=CREATED_AT,
        )
        draft_id = agent.seed_plan(
            version=2,
            status="draft",
            content=_plan_text(explanation="既有调整草案"),
            source_plan_id=active_id,
        )

        frames = agent.run(ADJUST_REQUEST, regenerate=True)

        assert _event_names(frames) == [
            *["node"] * len(ADJUST_NODE_SEQUENCE),
            "waiting",
            "done",
        ]
        assert _node_names(frames) == list(ADJUST_NODE_SEQUENCE)
        assert frames[-2][1] == {"draft_plan_id": draft_id}
        rows = agent.plans()
        assert [(row["id"], row["status"]) for row in rows] == [
            (active_id, "active"),
            (draft_id, "draft"),
        ]
        assert rows[1]["version"] == 2
        assert rows[1]["source_plan_id"] == active_id
        assert _content(rows[1])["explanation"] == "调整后"


def test_regenerate_failure_keeps_the_original_draft(tmp_path: Path) -> None:
    """§7.6：同类 regenerate 二次阻断失败时不改原 draft，也不写 rejected（失败候选只留 State）。"""
    scripts = {
        PLANNER_SYSTEM_PROMPT: [
            _plan_text(explanation="失败候选"),
            _plan_text(explanation="修订候选"),
        ],
        EVALUATOR_SYSTEM_PROMPT: [
            _rubric_text(goal_alignment=False),
            _rubric_text(goal_alignment=False),
        ],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()
        draft_id = agent.seed_plan(
            version=1, status="draft", content=_plan_text(explanation="既有草案")
        )
        before = agent.plans()

        frames = agent.run(REQUEST, regenerate=True)

        assert [name for name, _ in frames][-2:] == ["message", "done"]
        assert frames[-2][1] == {"text": REJECT_DRAFT_MESSAGE}
        assert frames[-1][1] == {
            "ok": True,
            "intent": "generate_plan",
            "termination_reason": "reject_draft",
            "draft_plan_id": draft_id,
        }
        assert agent.model.prompts == [
            PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
            PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
        ]
        assert agent.plans() == before


def test_existing_draft_conflicts_never_write(tmp_path: Path) -> None:
    """§3.4 第 5–6 条／§7.5：跨类型替换、普通 adjust 遇既有 draft、来源 active 已变化都是明确失败。"""
    cases = (
        # 已有调整 draft ＋ 生成请求：跨类型，两种 regenerate 取值都冲突。
        ("adjust_draft", REQUEST, False),
        ("adjust_draft", REQUEST, True),
        # 已有调整 draft ＋ 普通调整请求：明确失败。
        ("adjust_draft", ADJUST_REQUEST, False),
        # 已有生成 draft ＋ 调整请求：跨类型。
        ("generate_draft", ADJUST_REQUEST, True),
        # 已有生成 draft ＋ 普通调整请求：跨类型。
        ("generate_draft", ADJUST_REQUEST, False),
    )
    for kind, request, regenerate in cases:
        with _scripted_app(tmp_path / f"{kind}-{request}-{regenerate}") as agent:
            agent.seed_profile()
            active_id = agent.seed_plan(
                version=1,
                status="active",
                content=_plan_text(explanation="当前 active"),
                confirmed_at=CREATED_AT,
            )
            agent.seed_plan(
                version=2,
                status="draft",
                content=_plan_text(explanation="既有草案"),
                source_plan_id=active_id if kind == "adjust_draft" else None,
            )
            before = agent.plans()

            frames = agent.run(request, regenerate=regenerate)

            assert _event_names(frames)[-1] == "error", (kind, request, regenerate)
            assert frames[-1][1]["message"].startswith("已有")
            assert agent.model.calls == []
            assert agent.plans() == before


def test_adjust_regenerate_conflicts_when_the_source_is_no_longer_active(
    tmp_path: Path,
) -> None:
    """§3.4 第 6 条：既有调整 draft 的来源计划已不是当前 active 时明确冲突，不调模型、不写入。"""
    with _scripted_app(tmp_path) as agent:
        agent.seed_profile()
        agent.seed_plan(
            version=1,
            status="active",
            content=_plan_text(explanation="当前 active"),
            confirmed_at=CREATED_AT,
        )
        # 旧来源：一份已归档的上一版计划（不是当前 active）。
        old_active_id = agent.seed_plan(
            version=2, status="archived", content=_plan_text(explanation="上一版 active")
        )
        agent.seed_plan(
            version=3,
            status="draft",
            content=_plan_text(explanation="旧来源的草案"),
            source_plan_id=old_active_id,
        )
        before = agent.plans()

        frames = agent.run(ADJUST_REQUEST, regenerate=True)

        assert _event_names(frames) == ["error"]
        assert "来源计划已不是当前 active" in frames[-1][1]["message"]
        assert agent.model.calls == []
        assert agent.plans() == before


def test_run_request_shape_and_uuid_errors_are_json_before_the_stream(
    tmp_path: Path,
) -> None:
    """§3.7：请求 JSON 形状、字段类型或 UUID 非法在流建立前返回既有 JSON 错误形状。"""
    invalid_bodies: tuple[dict[str, Any], ...] = (
        {"conversation_id": "not-a-uuid", "request": REQUEST},
        {"conversation_id": CONVERSATION_ID},
        {"conversation_id": CONVERSATION_ID, "request": 42},
        {"conversation_id": CONVERSATION_ID, "request": REQUEST, "extra": 1},
    )
    with _scripted_app(tmp_path) as agent:
        agent.seed_profile()
        for body in invalid_bodies:
            response = agent.client.post("/api/agent/run", json=body)

            assert response.status_code == 400, body
            assert response.headers["content-type"].startswith("application/json")
            assert "event:" not in response.text
            payload = response.json()
            assert set(payload) == {"http_status", "error_code", "message"}
            assert payload["http_status"] == 400
            assert payload["error_code"] == "invalid_request"

        assert agent.model.calls == []
        assert agent.plans() == []


def test_run_errors_are_one_sse_error_and_never_mix_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.7：流建立后的运行错误只发一个 SSE ``error`` 后关闭，不混用 JSON。"""
    # 缺少画像：Planner 前明确失败，错误文本是本项目的产品文本。
    with _app_client(tmp_path / "no-profile") as client:
        frames = _sse_frames(
            client.post(
                "/api/agent/run",
                json={"conversation_id": CONVERSATION_ID, "request": REQUEST},
            )
        )

        assert _event_names(frames) == ["node", "error"]
        assert frames[-1][1] == {
            "message": "画像未建档：请先补充每周训练次数等必需事实后再生成计划"
        }

    # 缺模型配置：生产装配的惰性入口在首次调用时失败，同样只发一条 SSE error。
    for name in (MODEL_API_KEY_ENV, MODEL_BASE_URL_ENV, MODEL_MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    with _app_client(tmp_path / "no-model") as client:
        db: Database = client.app.state.db
        client.portal.call(partial(ProfileService(db).update, _profile()))

        response = client.post(
            "/api/agent/run",
            json={"conversation_id": CONVERSATION_ID, "request": REQUEST},
        )

        frames = _sse_frames(response)
        assert _event_names(frames)[-1] == "error"
        assert frames[-1][1] == {"message": MODEL_CONFIGURATION_ERROR_MESSAGE}
        assert "http_status" not in response.text
        assert client.portal.call(_plan_rows, db) == []


async def test_run_timeout_is_one_sse_error_without_secrets(tmp_path: Path) -> None:
    """§3.7／§7.6：Run 时限用尽（超时）同样只发一条 SSE ``error``，文本固定且不回显异常原文。

    生产装配不给 Run 时限留注入点（60／180／5 是冻结上限，§3.9 不新增配置），故只把本次 Run 的预算
    调小；其余全走真实实现——真实计划子图、真实 ``stream_agent_run``（Run 时限包住整次调用）与真实
    ``api/routes_agent.py::_sse_frames``（HTTP 报文形态由 ``test_run_errors_are_one_sse_error_and_never_mix_json``
    取证）。
    """
    leak = (
        f"{MODEL_API_KEY_ENV}=sk-timeout-secret "
        f"{MODEL_BASE_URL_ENV}=https://timeout.invalid/v1"
    )
    db = await _migrated(tmp_path / "fit_agent.db")
    try:
        await ProfileService(db).update(_profile())
        model = BlockingModel(leak)
        deps = _scripted_deps(db, model)
        async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
            events = stream_agent_run(
                build_generate_plan_graph(deps, checkpointer=saver),
                {"conversation_id": CONVERSATION_ID, "request": REQUEST},
                thread_config(CONVERSATION_ID),
                GeneratePlanRun(
                    business_day=BUSINESS_DAY,
                    budget=ModelRequestBudget(
                        run_timeout_seconds=TIMEOUT_RUN_SECONDS,
                        request_timeout_seconds=TIMEOUT_RUN_SECONDS,
                    ),
                ),
                AgentRunDeps(
                    model=model,
                    stats=StatsService(db),
                    plans=deps.plans,
                    persistence=deps.persistence,
                ),
            )
            body = "".join([frame async for frame in route_sse_frames(events)])

        frames = _parse_frames(body)
        assert _event_names(frames).count("error") == 1
        assert frames[-1] == ("error", {"message": AGENT_RUN_ERROR_MESSAGE})
        assert model.calls == 1  # 超时发生在 Planner 的模型调用里，不是没跑到计划链路就结束
        assert leak not in body
        assert await _plan_rows(db) == []
    finally:
        await db.close()


def test_run_error_coverage_maps_provider_errors_to_a_fixed_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.7 收尾条：Provider 异常（可能带端点／模型名／密钥）只回固定文本，不透传 ``str(exc)``。

    走生产装配：``api/app.py::_lazy_model_call`` 把 Provider／SDK 异常换成
    ``graph.model.ModelCallFailed``，响应与存档都不出现异常原文。
    """
    secret = f"https://{MODEL_BASE_URL_ENV}.invalid/v1 {MODEL_MODEL_ENV}=secret-model"
    monkeypatch.setattr(
        api_app, "openai_compatible_model_call", lambda: FailingModel(RuntimeError(secret))
    )
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        client.portal.call(partial(ProfileService(db).update, _profile()))

        response = client.post(
            "/api/agent/run",
            json={"conversation_id": CONVERSATION_ID, "request": REQUEST},
        )

        frames = _sse_frames(response)
        assert _event_names(frames)[-1] == "error"
        assert frames[-1][1] == {"message": MODEL_CALL_FAILED_MESSAGE}
        assert secret not in response.text
        assert client.portal.call(_plan_rows, db) == []


def test_run_stream_and_archive_carry_no_secrets_or_provider_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.7／§3.8：响应字节与 checkpoint 存档都不含密钥、端点、模型名或环境变量名。

    Provider 失败（可能带 Base URL／模型名）是唯一会写进存档的错误文本来源：生产模型入口已把
    异常换成固定文本，因此存档里只剩固定文本。
    """
    api_key = "stage5-agent-api-key-must-not-leak"
    base_url = "https://stage5-agent-provider.invalid/v1"
    model_name = "stage5-agent-model-must-not-leak"
    monkeypatch.setattr(
        api_app,
        "openai_compatible_model_call",
        lambda: FailingModel(
            OSError(f"connect {base_url} with {api_key} for {model_name}")
        ),
    )
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        client.portal.call(partial(ProfileService(db).update, _profile()))

        response = client.post(
            "/api/agent/run",
            json={"conversation_id": CONVERSATION_ID, "request": REQUEST},
        )

        frames = _sse_frames(response)
        assert _event_names(frames)[-1] == "error"
        assert frames[-1][1] == {"message": MODEL_CALL_FAILED_MESSAGE}
        for forbidden in (
            api_key,
            base_url,
            model_name,
            MODEL_API_KEY_ENV,
            MODEL_BASE_URL_ENV,
            MODEL_MODEL_ENV,
            # 提示词与原始模型文本都不进事件。
            PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
        ):
            assert forbidden not in response.text, forbidden
        data_dir = db.path.parent

    # lifespan 退出后读存档：WAL 已 checkpoint，读到的就是落盘字节。
    archive = checkpoint_database_path(data_dir).read_bytes()
    assert CONVERSATION_ID.encode() in archive  # 非空断言：读到的是真实存档
    for forbidden in (
        api_key,
        base_url,
        model_name,
        MODEL_API_KEY_ENV,
        MODEL_BASE_URL_ENV,
        MODEL_MODEL_ENV,
    ):
        assert forbidden.encode() not in archive


# ---------- §7.4 兜底：确认／拒绝的 HTTP 行为 ----------


def _waiting_draft(agent: _AgentApp) -> int:
    """跑一次生成把 draft 落到等待确认，返回 draft 身份。"""
    frames = agent.run(REQUEST)
    return int(
        next(data["draft_plan_id"] for name, data in frames if name == "waiting")
    )


def test_confirm_over_http_activates_the_draft_and_stays_idempotent(tmp_path: Path) -> None:
    """§7.4：等待中的 checkpoint ＋ ID 相等时 resume 成功；重复确认走领域幂等，不产生第二条 active。"""
    scripts = {
        PLANNER_SYSTEM_PROMPT: [_plan_text()],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()
        draft_id = _waiting_draft(agent)

        first = agent.client.post(
            "/api/agent/confirm",
            json={"conversation_id": CONVERSATION_ID, "plan_id": draft_id},
        )
        assert first.status_code == 200, first.text
        assert first.json()["plan"]["id"] == draft_id
        assert first.json()["plan"]["status"] == "active"
        assert first.json()["plan"]["confirmed_at"] == FIXED_NOW.isoformat()

        second = agent.client.post(
            "/api/agent/confirm",
            json={"conversation_id": CONVERSATION_ID, "plan_id": draft_id},
        )
        assert second.status_code == 200
        assert second.json() == first.json()
        rows = agent.plans()
        assert [(row["id"], row["status"]) for row in rows] == [(draft_id, "active")]


def test_confirmation_with_a_mismatched_plan_id_is_409_without_writes(tmp_path: Path) -> None:
    """§7.4／§3.7：interrupt 的 draft ID 与请求 ``plan_id`` 不等时 409，且不写任何行。"""
    scripts = {
        PLANNER_SYSTEM_PROMPT: [_plan_text()],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()
        draft_id = _waiting_draft(agent)
        before = agent.plans()

        for action in ("confirm", "reject"):
            response = agent.client.post(
                f"/api/agent/{action}",
                json={"conversation_id": CONVERSATION_ID, "plan_id": draft_id + 99},
            )

            assert response.status_code == 409, response.text
            assert response.json()["error_code"] == "invalid_request"
            assert agent.plans() == before
            assert draft_id == before[0]["id"] and before[0]["status"] == "draft"


def test_confirmation_of_a_missing_plan_is_404_without_writes(tmp_path: Path) -> None:
    """§3.7：``confirm``／``reject`` 的计划不存在时是 404，且不写任何行。"""
    with _scripted_app(tmp_path) as agent:
        agent.seed_profile()
        for action in ("confirm", "reject"):
            response = agent.client.post(
                f"/api/agent/{action}",
                json={"conversation_id": OTHER_CONVERSATION_ID, "plan_id": 999},
            )

            assert response.status_code == 404, response.text
            assert response.json()["error_code"] == "invalid_request"
        assert agent.plans() == []


def test_reject_over_http_archives_the_draft_and_never_writes_rejected(
    tmp_path: Path,
) -> None:
    """§7.3／§7.4：拒绝把 draft 归档，原 active 不变，库内不出现 ``rejected`` 状态。"""
    scripts = {
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [_plan_text(explanation="调整后")],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_profile()
        active_id = agent.seed_plan(
            version=1,
            status="active",
            content=_plan_text(explanation="当前 active"),
            confirmed_at=CREATED_AT,
        )
        frames = agent.run(ADJUST_REQUEST)
        draft_id = next(data["draft_plan_id"] for name, data in frames if name == "waiting")
        active_before = [row for row in agent.plans() if row["status"] == "active"]

        response = agent.client.post(
            "/api/agent/reject",
            json={"conversation_id": CONVERSATION_ID, "plan_id": draft_id},
        )

        assert response.status_code == 200, response.text
        assert response.json()["plan"]["status"] == "archived"
        assert response.json()["plan"]["archived_at"] == FIXED_NOW.isoformat()
        rows = agent.plans()
        assert [row["status"] for row in rows] == ["active", "archived"]
        assert [row for row in rows if row["status"] == "active"] == active_before
        assert rows[1]["id"] == draft_id
        assert rows[1]["source_plan_id"] == active_id
        # 重复拒绝是幂等成功：返回同一归档行，不写第二条。
        repeat = agent.client.post(
            "/api/agent/reject",
            json={"conversation_id": CONVERSATION_ID, "plan_id": draft_id},
        )
        assert repeat.status_code == 200
        assert repeat.json() == response.json()
        assert agent.plans() == rows


# ---------- §7.6 断线：不是取消、确认或拒绝信号 ----------


async def test_closing_the_event_stream_writes_nothing_and_keeps_the_draft(
    tmp_path: Path,
) -> None:
    """§3.8／§7.6：客户端在 ``waiting`` 后断开时不触发确认／拒绝或额外写入，也不回滚已持久化 draft。"""
    db = await _migrated(tmp_path / "fit_agent.db")
    try:
        await ProfileService(db).update(_profile())
        model = ScriptedModel(
            {
                PLANNER_SYSTEM_PROMPT: [_plan_text()],
                EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
            }
        )
        deps = _scripted_deps(db, model)
        run_deps = AgentRunDeps(
            model=model,
            stats=StatsService(db),
            plans=deps.plans,
            persistence=deps.persistence,
        )
        async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
            graph = build_generate_plan_graph(deps, checkpointer=saver)
            events = stream_agent_run(
                graph,
                {"conversation_id": CONVERSATION_ID, "request": REQUEST},
                thread_config(CONVERSATION_ID),
                GeneratePlanRun(business_day=BUSINESS_DAY),
                run_deps,
            )
            seen: list[AgentEvent] = []
            async for event in events:
                seen.append(event)
                if event.event == "waiting":
                    break
            await events.aclose()

            assert seen[-1].event == "waiting"
            rows = await _plan_rows(db)
            assert [row["status"] for row in rows] == ["draft"]
            assert rows[0]["confirmed_at"] is None
            assert rows[0]["archived_at"] is None
            # 图仍停在确认 interrupt：断线既没确认也没拒绝。
            snapshot = await graph.aget_state(thread_config(CONVERSATION_ID))
            assert snapshot.next == ("wait_for_confirmation",)
            assert snapshot.interrupts[0].value == {"draft_plan_id": rows[0]["id"]}
    finally:
        await db.close()


# ---------- §3.5 安全优先：不读 active、不读 draft ----------


class _CountingPlanReads(PlanReadService):
    """只读计划入口计数替身：取证「安全命中时整次 Run 都没有读 active」（§3.5 优先于 §3.4 预读）。"""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.active_reads = 0

    async def get_active(self) -> Plan | None:
        self.active_reads += 1
        return await super().get_active()


class _CountingDraftReads(PlanPersistenceService):
    """draft 只读入口计数替身：取证「安全命中时整次 Run 都没有读唯一 draft」（§3.5 优先于 §3.4 第 5–6 条）。"""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.draft_reads = 0

    async def get_unique_draft(self) -> Plan | None:
        self.draft_reads += 1
        return await super().get_unique_draft()


async def test_run_red_flag_never_reads_the_active_plan(tmp_path: Path) -> None:
    """§3.5／A1：安全命中先于 adjust 的 active 预读——既有调整 draft ＋ regenerate 也不读 active、不写入。"""
    db = await _migrated(tmp_path / "fit_agent.db")
    try:
        await ProfileService(db).update(_profile())
        model = ScriptedModel()
        reading = _CountingPlanReads(db)
        deps = replace(_scripted_deps(db, model), plans=reading)
        active_id = await _insert_plan(
            db,
            version=1,
            status="active",
            content=_plan_text(explanation="当前 active"),
            confirmed_at=CREATED_AT,
        )
        await _insert_plan(
            db,
            version=2,
            status="draft",
            content=_plan_text(explanation="既有调整草案"),
            source_plan_id=active_id,
        )
        before = await _plan_rows(db)

        async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
            graph = build_generate_plan_graph(deps, checkpointer=saver)
            events = stream_agent_run(
                graph,
                {
                    "conversation_id": CONVERSATION_ID,
                    "request": RED_FLAG_ADJUST_REQUEST,
                },
                thread_config(CONVERSATION_ID),
                GeneratePlanRun(business_day=BUSINESS_DAY, regenerate=True),
                AgentRunDeps(
                    model=model,
                    stats=StatsService(db),
                    plans=reading,
                    persistence=deps.persistence,
                ),
            )
            seen = [event async for event in events]

        assert [(event.event, event.data) for event in seen] == [
            ("node", {"name": "safety_check"}),
            ("node", {"name": "safety_stop"}),
            ("message", {"text": SAFETY_STOP_MESSAGE}),
            (
                "done",
                {
                    "ok": True,
                    "intent": None,
                    "termination_reason": "safety_stop",
                    "draft_plan_id": None,
                },
            ),
        ]
        assert reading.active_reads == 0
        assert model.calls == []
        assert await _plan_rows(db) == before
    finally:
        await db.close()


async def test_run_red_flag_never_reads_the_existing_draft(tmp_path: Path) -> None:
    """A1／§3.5：安全命中先于 §3.4 第 5–6 条的已有 draft 判定——有同类既有 draft 也不读它、不写入。

    修复前普通生成请求会先调 ``get_unique_draft`` 再决定复用（或同类 regenerate 替换）；预检提前到
    Router 之前后一次 draft 读都不发生，替身计数取证 ``draft_reads == 0``。
    """
    db = await _migrated(tmp_path / "fit_agent.db")
    try:
        await ProfileService(db).update(_profile())
        model = ScriptedModel()
        drafts = _CountingDraftReads(db)
        deps = replace(_scripted_deps(db, model), persistence=drafts)
        await _insert_plan(
            db, version=1, status="draft", content=_plan_text(explanation="既有草案")
        )
        before = await _plan_rows(db)

        async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
            graph = build_generate_plan_graph(deps, checkpointer=saver)
            events = stream_agent_run(
                graph,
                {
                    "conversation_id": CONVERSATION_ID,
                    "request": RED_FLAG_REQUEST,
                },
                thread_config(CONVERSATION_ID),
                GeneratePlanRun(business_day=BUSINESS_DAY),
                AgentRunDeps(
                    model=model,
                    stats=StatsService(db),
                    plans=deps.plans,
                    persistence=drafts,
                ),
            )
            seen = [event async for event in events]

        assert [(event.event, event.data) for event in seen] == [
            ("node", {"name": "safety_check"}),
            ("node", {"name": "safety_stop"}),
            ("message", {"text": SAFETY_STOP_MESSAGE}),
            (
                "done",
                {
                    "ok": True,
                    "intent": None,
                    "termination_reason": "safety_stop",
                    "draft_plan_id": None,
                },
            ),
        ]
        assert drafts.draft_reads == 0
        assert model.calls == []
        assert await _plan_rows(db) == before
    finally:
        await db.close()
