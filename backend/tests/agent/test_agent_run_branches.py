# 一次 Agent Run 的非工具分支行为：表单引导、自然语言打卡、一般对话与计划链路的可见输出、
# 只读边界与预算，非法路由组合在进入任何分支前失败。
# 依据：本轮拍板的 FitnessIntent Schema；日程与进展两个只读工具分支在 test_agent_tool_branches.py。
# 计划事实用 tmp_path 下的真实迁移库，模型是可脚本化的固定替身，不调真实模型。

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ValidationError

from app.application.agent.budget import (
    ModelRequestBudget,
    ModelRequestBudgetExceeded,
)
from app.application.agent.contracts import (
    LOCAL_USER_ID,
    AgentEvent,
    AgentRunDeps,
    AgentRunResult,
    GeneratePlanDeps,
    GeneratePlanRun,
    Intent,
    PlanDeterministicDeps,
    PlanLlmNodeDeps,
    PlanWriteDeps,
    WorkflowState,
    initial_workflow_state,
    thread_config,
)
from app.application.agent.harness.registry import (
    PLANNING_TOOLS,
    UnregisteredCandidateExercise,
    build_general_tool_harnesses,
    build_plan_tool_harnesses,
)
from app.application.agent.harness.snapshot import MissingPlanFacts
from app.application.agent.harness.tools.exercise_dataset.store import (
    InMemoryCanonicalExerciseDataset,
)
from app.application.agent.harness.tools.prepare_workout_record import ExtractedWorkout
from app.application.agent.plan_graph import build_generate_plan_graph
from app.application.agent.prompts import (
    EVALUATOR_SYSTEM_PROMPT,
    GENERAL_CHAT_SYSTEM_PROMPT,
    NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
    NATURAL_LANGUAGE_RECORD_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    SAFETY_STOP_MESSAGE,
)
from app.application.agent.router import ROUTER_SYSTEM_PROMPT, FitnessIntent
from app.application.agent.run_service import (
    invoke_agent_run,
    stream_agent_run,
)
from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.domain.conversations.context import ContextMessage
from app.domain.plans.schema import PlanDraft, RubricResult
from app.domain.profile.schema import Fact, Profile
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.plans_repository import PlanRepo
from app.infrastructure.langgraph.checkpoints import open_checkpointer
from app.infrastructure.skills.loader import SkillLoader
from config import MAX_MODEL_REQUESTS_PER_RUN, TOOL_TIMEOUT_SECONDS, skills_dir

BUSINESS_DAY = date(2026, 6, 1)
SCHEDULED_ON = "2026-06-01"
NEXT_DAY = "2026-06-02"
CONVERSATION_ID = "router-branch-conversation"
RUN_ID = "router-branch-run"
CLIENT_REQUEST_ID = "router-branch-client-request"
CREATED_AT = "2026-06-01T08:00:00+00:00"
PULL_UP = "pull-up"
PLANK = "plank"
PROFILE_WEEKLY_FREQUENCY = 1
ANSWER = "固定替身答复"
SUMMARY = "固定替身摘要"
FACTS_READ = "事实已读完"

#: 计划路径的事实采集脚本：必需工具与 production 的 ``PLAN_REQUIRED_FACTS`` 同集。
GENERATE_FACT_TOOLS = (
    "read_user_profile",
    "read_training_history",
    "read_progress",
    "search_exercises",
)
ADJUST_FACT_TOOLS = (*GENERATE_FACT_TOOLS, "read_active_plan", "read_training_calendar")

#: 三个分支共用的业务库行数快照口径：任何一张业务表变化即失败。
COUNTED_TABLES = (
    "plans",
    "plan_sessions",
    "workout_sessions",
    "workout_sets",
    "body_metrics",
)


@dataclass(frozen=True, slots=True)
class HarnessCall:
    """一次 harness 模型调用的记录：offered 工具名与收到的完整消息序列。"""

    offered: tuple[str, ...]
    messages: tuple[BaseMessage, ...]


@dataclass
class ScriptedGateway:
    """固定替身模型入口：结构化按 Schema 出队，文本按系统提示词出队，工具响应按序出队。"""

    structured_scripts: dict[type[BaseModel], list[Mapping[str, Any]]] = field(
        default_factory=dict
    )
    text_scripts: dict[str, list[str]] = field(default_factory=dict)
    harness_scripts: list[AIMessage] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    harness_calls: list[HarnessCall] = field(default_factory=list)

    async def text(self, system_prompt: str, user_payload: str) -> str:
        self.calls.append((system_prompt, user_payload))
        scripts = self.text_scripts.get(system_prompt)
        if not scripts:
            raise AssertionError(f"固定替身收到未脚本化的文本调用：{system_prompt[:24]!r}")
        return scripts.pop(0)

    async def structured(
        self,
        system_prompt: str,
        user_payload: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        self.calls.append((system_prompt, user_payload))
        scripts = self.structured_scripts.get(schema)
        if not scripts:
            raise AssertionError(f"固定替身收到未脚本化的结构化调用：{schema.__name__}")
        return schema.model_validate(scripts.pop(0))

    async def tools(
        self, messages: Sequence[BaseMessage], offered_tools: Sequence[BaseTool]
    ) -> AIMessage:
        self.harness_calls.append(
            HarnessCall(
                tuple(tool.name for tool in offered_tools), tuple(messages)
            )
        )
        if not self.harness_scripts:
            raise AssertionError("固定替身收到未脚本化的工具调用请求")
        return self.harness_scripts.pop(0)

    def text_calls(self) -> list[str]:
        """文本调用的系统提示词序列（结构化路由调用不计入）。"""
        return [
            prompt
            for prompt, _ in self.calls
            if prompt
            not in (ROUTER_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT, EVALUATOR_SYSTEM_PROMPT)
        ]

    def payload_for(self, system_prompt: str) -> dict[str, Any]:
        """某一类文本调用的用户载荷（JSON 文本解码）。"""
        for prompt, payload in self.calls:
            if prompt == system_prompt:
                return json.loads(payload)
        raise AssertionError(f"没有该提示词的调用：{system_prompt[:24]!r}")


def tool_call(
    name: str, args: Mapping[str, Any] | None = None, *, call_id: str = "call-1"
) -> AIMessage:
    """脚本化一次工具调用：content 为空，``tool_calls`` 恰一条。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": dict(args or {}),
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def final_answer(text: str) -> AIMessage:
    """脚本化一次不带工具调用的最终答复：harness 循环由此进入 END。"""
    return AIMessage(content=text)


def _fact_args(name: str, *, query: str) -> dict[str, Any]:
    """一次只读工具调用的参数：与目标工具的模型可见 Schema 一致。"""
    if name == "search_exercises":
        return {"query": query}
    if name == "read_training_calendar":
        return {"year": BUSINESS_DAY.year, "month": BUSINESS_DAY.month}
    return {}


def _fact_loop(*tools: str, query: str = "引体") -> list[AIMessage]:
    """一次事实采集 loop 的脚本：一条并行工具调用 ＋ 一句收尾；工具结果来自真实 ToolNode。"""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": name,
                    "args": _fact_args(name, query=query),
                    "id": f"call-{index}",
                    "type": "tool_call",
                }
                for index, name in enumerate(tools)
            ],
        ),
        final_answer(FACTS_READ),
    ]


def _fact_loops(count: int, *tools: str) -> list[AIMessage]:
    """同一条事实采集 loop 重复若干次：修订路径上 Planner 与 Evaluator 各跑一遍。"""
    return [message for _ in range(count) for message in _fact_loop(*tools)]


def executed_tools(call: HarnessCall) -> tuple[str, ...]:
    """一次 harness 调用消息序列里已执行的工具名：只取 ToolNode 实际返回的 ToolMessage。"""
    return tuple(
        message.name
        for message in call.messages
        if isinstance(message, ToolMessage) and message.name is not None
    )


def _tool_harnesses() -> Mapping[Intent, CompiledStateGraph]:
    """与 ``build_agent_runtime()`` 同形的测试装配：五项会话 Intent 各一份按白名单固化的 harness。"""
    return build_general_tool_harnesses(
        skills=SkillLoader(skills_dir()), timeout_seconds=TOOL_TIMEOUT_SECONDS
    )


@dataclass
class Harness:
    """一次测试的图、业务库、固定替身与 Run 依赖，共用唯一运行入口。"""

    graph: CompiledStateGraph
    db: Database
    model: ScriptedGateway
    run_deps: AgentRunDeps

    def run_state(self, request: str) -> WorkflowState:
        """与 ``run_events`` 相同的 Run 初始状态：身份与业务日由 API 边界注入。"""
        return initial_workflow_state(
            run_id=RUN_ID,
            conversation_id=CONVERSATION_ID,
            user_id=LOCAL_USER_ID,
            client_request_id=CLIENT_REQUEST_ID,
            business_day=BUSINESS_DAY,
            request=request,
        )

    async def invoke(
        self,
        request: str,
        *,
        budget: ModelRequestBudget | None = None,
        history: Sequence[ContextMessage] = (),
    ) -> AgentRunResult:
        run = self.run_context(budget=budget, history=history)
        return await invoke_agent_run(
            self.graph,
            self.run_state(request),
            thread_config(CONVERSATION_ID),
            run,
            self.run_deps,
        )

    async def stream(
        self,
        request: str,
        *,
        budget: ModelRequestBudget | None = None,
    ) -> list[AgentEvent]:
        """一次 Run 的完整产品事件序列：与 ``run_events`` 走同一个 ``stream_agent_run``。"""
        run = self.run_context(budget=budget)
        return [
            event
            async for event in stream_agent_run(
                self.graph,
                self.run_state(request),
                thread_config(CONVERSATION_ID),
                run,
                self.run_deps,
            )
        ]

    def run_context(
        self,
        *,
        budget: ModelRequestBudget | None = None,
        history: Sequence[ContextMessage] = (),
    ) -> GeneratePlanRun:
        """一次 Run 的运行上下文：业务日、共享预算与本次请求之前的上下文投影。"""
        return GeneratePlanRun(
            business_day=BUSINESS_DAY,
            budget=ModelRequestBudget() if budget is None else budget,
            conversation_messages=tuple(history),
        )


def _plan_llm_deps(
    model, repositories, services, schema_version, dataset, *, evaluation
):
    """一次 LLM Node 的构造期依赖：唯一模型入口 ＋ 该路径自己的 ToolNode 与只读端口。"""
    harnesses = build_plan_tool_harnesses(timeout_seconds=TOOL_TIMEOUT_SECONDS)
    return PlanLlmNodeDeps(
        model=model,
        harness=harnesses.evaluation if evaluation else harnesses.planning,
        profiles=repositories.profiles,
        records=services.records,
        catalog=repositories.exercises,
        plans=repositories.plans,
        stats=services.stats,
        revisions=repositories.tool_cache,
        schema_version=schema_version,
        dataset=dataset,
    )


@asynccontextmanager
async def _harness(
    tmp_path: Path,
    *,
    structured: Mapping[type[BaseModel], Sequence[Mapping[str, Any]]] | None = None,
    text: Mapping[str, Sequence[str]] | None = None,
    harness: Sequence[AIMessage] = (),
) -> AsyncIterator[Harness]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        repositories = build_repositories(db)
        services = build_services(
            repositories, db, SqliteHealthProbe(db, db.path.parent)
        )
        skills = SkillLoader(skills_dir())
        await services.profile.update(_profile())
        user_version = await db.pragma_value("user_version")
        if not isinstance(user_version, int):
            raise RuntimeError(f"迁移后 user_version 不是整数：{user_version!r}")
        dataset = InMemoryCanonicalExerciseDataset(
            await repositories.exercises.list_all(), catalog_revision=user_version
        )
        model = ScriptedGateway(
            structured_scripts={
                schema: [dict(row) for row in rows]
                for schema, rows in (structured or {}).items()
            },
            text_scripts={
                prompt: list(items) for prompt, items in (text or {}).items()
            },
            harness_scripts=list(harness),
        )
        deps = GeneratePlanDeps(
            planner=_plan_llm_deps(
                model, repositories, services, user_version, dataset, evaluation=False
            ),
            evaluator=_plan_llm_deps(
                model, repositories, services, user_version, dataset, evaluation=True
            ),
            deterministic=PlanDeterministicDeps(
                profiles=repositories.profiles,
                catalog=repositories.exercises,
                plans=repositories.plans,
                stats=services.stats,
            ),
            writes=PlanWriteDeps(
                plans=services.plan_writes,
                now=lambda: datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            ),
            skills=skills,
        )
        async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
            yield Harness(
                graph=build_generate_plan_graph(deps, checkpointer=saver),
                db=db,
                model=model,
                run_deps=AgentRunDeps(
                    model=model,
                    stats=services.stats,
                    plans=repositories.plans,
                    plan_writes=services.plan_writes,
                    catalog=repositories.exercises,
                    records=services.records,
                    profiles=repositories.profiles,
                    skills=skills,
                    tool_harnesses=_tool_harnesses(),
                    revisions=repositories.tool_cache,
                    schema_version=user_version,
                    dataset=dataset,
                ),
            )
    finally:
        await db.close()


def _profile() -> Profile:
    """固定画像：每周训练次数是明确值（计划链路的前置）。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=Fact.known(PROFILE_WEEKLY_FREQUENCY),
        available_equipment=Fact.known(("barbell", "bodyweight")),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.denied(),
        forbidden_exercise_ids=Fact.denied(),
    )


def _route(**fields: Any) -> dict[type[BaseModel], list[Mapping[str, Any]]]:
    """给一次运行预设 Router 的结构化响应。"""
    return {FitnessIntent: [fields]}


def _schedule_plan_content() -> dict[str, Any]:
    """两练的 active 计划：负重动作（含已知负荷）、计时动作与自重动作各一。"""
    return {
        "goal": "增肌",
        "starts_on": SCHEDULED_ON,
        "explanation": "每周两练",
        "weekly_frequency": 2,
        "training_days": [
            {
                "scheduled_on": SCHEDULED_ON,
                "exercises": [
                    {
                        "exercise_id": "barbell-back-squat",
                        "sets": 3,
                        "prescription": {
                            "type": "weighted_reps",
                            "reps_min": 5,
                            "reps_max": 8,
                            "progression_note": None,
                            "load": {
                                "status": "known",
                                "weight_kg": 60.0,
                                "source_workout_session_id": 1,
                                "source_set_no": 1,
                            },
                        },
                    },
                    {
                        "exercise_id": PLANK,
                        "sets": 2,
                        "prescription": {
                            "type": "timed",
                            "duration_seconds_min": 45,
                            "duration_seconds_max": 60,
                            "progression_note": None,
                        },
                    },
                ],
            },
            {
                "scheduled_on": NEXT_DAY,
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
            },
        ],
    }


def _bodyweight_plan_content() -> dict[str, Any]:
    """单练的自重计划：调整分支的 active 事实（无负荷目标，不受渐进决策约束）。"""
    return {
        "goal": "增肌",
        "starts_on": SCHEDULED_ON,
        "explanation": "每周一练",
        "weekly_frequency": 1,
        "training_days": [
            {
                "scheduled_on": NEXT_DAY,
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
    }


def _rubric_result(*, goal_alignment: bool) -> Mapping[str, Any]:
    """Evaluator 结构化响应：三个维度的布尔判定与理由。"""
    verdict = {"passed": True, "reason": "固定替身判定"}
    return {
        "goal_alignment": {"passed": goal_alignment, "reason": "固定替身判定"},
        "schedule_reasonableness": dict(verdict),
        "explanation_quality": dict(verdict),
    }


def _plan_scripts(
    goal_alignment: Sequence[bool],
) -> dict[type[BaseModel], list[Mapping[str, Any]]]:
    """计划链路的结构化响应：Planner 的两次草案与 Evaluator 的逐次 Rubric 判定。"""
    return {
        PlanDraft: [_bodyweight_plan_content(), _bodyweight_plan_content()],
        RubricResult: [
            _rubric_result(goal_alignment=passed) for passed in goal_alignment
        ],
    }


def _extraction_scripts() -> dict[type[BaseModel], list[Mapping[str, Any]]]:
    """自然语言打卡提取的结构化响应：一次自重组事实。"""
    return {
        ExtractedWorkout: [
            {
                "performed_on": SCHEDULED_ON,
                "sets": [
                    {
                        "exercise_id": PULL_UP,
                        "set_no": 1,
                        "set_type": "work",
                        "reps": 8,
                    }
                ],
            }
        ]
    }


async def _insert_active_plan(db: Database, content: Mapping[str, Any]) -> int:
    """直接 SQL 预置一个 active 计划行（正式写入入口是被测服务）。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, source_plan_id, structured_content,"
            " created_at, confirmed_at) VALUES (1, 'active', NULL, ?, ?, ?)",
            (json.dumps(content, ensure_ascii=False), CREATED_AT, CREATED_AT),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _row_counts(db: Database) -> dict[str, int]:
    """只读分支的断言用业务行数快照。"""

    async def op(conn: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in COUNTED_TABLES:
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                counts[table] = int((await cursor.fetchone())[0])
        return counts

    return await db.under_lock(op)


async def test_form_record_request_guides_to_the_form_without_writing(
    tmp_path: Path,
) -> None:
    """``workout_execution/create/form_record``：经 general_tools 取表单，不写库、不进计划子图。"""
    async with _harness(
        tmp_path,
        structured=_route(
            domain="workout_execution",
            action="create",
            execution_type="form_record",
        ),
        harness=[
            tool_call("get_workout_record_form"),
            final_answer(ANSWER),
        ],
    ) as h:
        before = await _row_counts(h.db)
        result = await h.invoke("打开打卡表单")

        assert result.intent == "form_record"
        assert result.messages == (ANSWER,)
        assert result.draft_plan_id is None
        assert h.model.harness_calls[0].offered == ("get_workout_record_form",)
        assert h.model.text_calls() == []
        assert await _row_counts(h.db) == before


@pytest.mark.parametrize(
    "request_text",
    [
        "你好",
        "杠铃平板卧推的动作规范、发力机制与轨迹是什么？",
        "新手一周练几次合适？",
    ],
)
async def test_general_chat_answers_with_one_harness_call(
    tmp_path: Path, request_text: str
) -> None:
    """一般对话与训练知识类问法：对话提示词作 harness system，白名单只读 Tool 已挂载、调用由模型自选。"""
    async with _harness(
        tmp_path,
        structured=_route(domain="general", action="chat"),
        harness=[final_answer(ANSWER)],
    ) as h:
        before = await _row_counts(h.db)
        result = await h.invoke(request_text)

        assert result.intent == "general"
        assert result.messages == (ANSWER,)
        assert result.draft_plan_id is None
        call = h.model.harness_calls[0]
        assert call.offered[:3] == (
            "search_exercises",
            "read_training_history",
            "read_active_plan",
        )
        assert call.offered[3:] == ("read_skill", "read_skill_reference")
        system_prompt = str(call.messages[0].content)
        assert system_prompt.startswith(GENERAL_CHAT_SYSTEM_PROMPT)
        assert all(
            name in system_prompt
            for name in ("fitness-knowledge", "exercise-guidance", "strength-training")
        )
        assert [message.content for message in call.messages].count(request_text) == 1
        assert h.model.text_calls() == []
        assert await _row_counts(h.db) == before


async def test_natural_language_record_still_extracts_without_writing(
    tmp_path: Path,
) -> None:
    """回归：自然语言打卡经 prepare_workout_record ToolNode 出候选与可见文本，确认前不写业务库。"""
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [
                {
                    "domain": "workout_execution",
                    "action": "create",
                    "execution_type": "natural_language_record",
                }
            ],
            **_extraction_scripts(),
        },
        harness=[
            tool_call("prepare_workout_record", {"request": "记录今天做8个引体"}),
            final_answer(SUMMARY),
        ],
    ) as h:
        before = await _row_counts(h.db)
        result = await h.invoke("记录今天做8个引体")

        assert result.intent == "natural_language_record"
        assert result.messages == (SUMMARY,)
        assert result.draft_plan_id is None
        call = h.model.harness_calls[0]
        assert call.offered[:2] == ("prepare_workout_record", "search_exercises")
        assert call.offered[2:] == ("read_skill", "read_skill_reference")
        system_prompt = str(call.messages[0].content)
        assert system_prompt.startswith(
            NATURAL_LANGUAGE_RECORD_SYSTEM_PROMPT.format(business_day=SCHEDULED_ON)
        )
        assert "workout-logging" in system_prompt
        assert [prompt for prompt, _ in h.model.calls] == [
            ROUTER_SYSTEM_PROMPT,
            NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
        ]
        assert await _row_counts(h.db) == before


async def test_natural_language_record_extraction_receives_the_run_history(
    tmp_path: Path,
) -> None:
    """历史投影进 harness 消息序列，也由运行上下文供 Tool 内的提取载荷使用，且本请求只出现一次。"""
    history = (
        ContextMessage(role="user", text="上一轮问题"),
        ContextMessage(role="assistant", text="上一轮回答"),
    )
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [
                {
                    "domain": "workout_execution",
                    "action": "create",
                    "execution_type": "natural_language_record",
                }
            ],
            **_extraction_scripts(),
        },
        harness=[
            tool_call("prepare_workout_record", {"request": "记录今天做8个引体"}),
            final_answer(SUMMARY),
        ],
    ) as h:
        result = await h.invoke("记录今天做8个引体", history=history)
        messages = h.model.harness_calls[0].messages
        structured_payload = h.model.payload_for(
            NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT
        )

        assert result.messages == (SUMMARY,)
        assert [type(message).__name__ for message in messages] == [
            "SystemMessage",
            "HumanMessage",
            "AIMessage",
            "HumanMessage",
        ]
        assert [message.content for message in messages[1:]] == [
            "上一轮问题",
            "上一轮回答",
            "记录今天做8个引体",
        ]
        assert structured_payload["request"] == "记录今天做8个引体"
        assert structured_payload["conversation_messages"] == [
            {"role": "user", "text": "上一轮问题"},
            {"role": "assistant", "text": "上一轮回答"},
        ]


async def test_generate_plan_keeps_the_plan_chain_and_the_run_budget(
    tmp_path: Path,
) -> None:
    """回归：生成计划仍走计划子图；最坏路径（路由 ＋ 两次规划 ＋ 两次 Rubric）恰用满 5 次 Run 预算。

    计划路径的两个 ToolNode loop 各有自己的事实预算，不占用这 5 次；工具结果来自真实迁移库。
    """
    budget = ModelRequestBudget()
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [{"domain": "plan_management", "action": "create"}],
            **(_plan_scripts(goal_alignment=[False, True])),
        },
        harness=_fact_loops(4, *GENERATE_FACT_TOOLS),
    ) as h:
        result = await h.invoke("帮我生成一份训练计划", budget=budget)

        assert [prompt for prompt, _ in h.model.calls] == [
            ROUTER_SYSTEM_PROMPT,
            PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
            PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
        ]
        evaluator_skills = [
            json.loads(payload)["skill"]
            for prompt, payload in h.model.calls
            if prompt == EVALUATOR_SYSTEM_PROMPT
        ]
        assert len(evaluator_skills) == 2
        assert evaluator_skills[0] == evaluator_skills[1]
        assert evaluator_skills[0]["name"] == "plan-evaluation"
        assert set(evaluator_skills[0]) == {
            "name", "instructions", "rules_reference"
        }
        assert budget.used == MAX_MODEL_REQUESTS_PER_RUN
        assert budget.tool_calls == 0
        assert result.intent == "generate_plan"
        assert result.draft_plan_id is not None
        written = await PlanRepo(h.db).read_by_id(result.draft_plan_id)
        assert written is not None and written.source_plan_id is None

    with pytest.raises(ModelRequestBudgetExceeded):
        budget.begin_request()


async def test_plan_path_runs_two_independent_tool_loops(tmp_path: Path) -> None:
    """Planner 与 Evaluator 各跑一个独立 ToolNode loop：白名单、节点名与调用轨迹各自独立。"""
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [{"domain": "plan_management", "action": "create"}],
            **_plan_scripts(goal_alignment=[True]),
        },
        harness=_fact_loops(2, *GENERATE_FACT_TOOLS),
    ) as h:
        result = await h.invoke("帮我生成一份训练计划")

        assert result.draft_plan_id is not None
        planner_call, planner_answer, evaluator_call, evaluator_answer = (
            h.model.harness_calls
        )
        assert planner_call.offered == tuple(tool.name for tool in PLANNING_TOOLS)
        assert planner_answer.offered == planner_call.offered
        assert evaluator_call.offered == tuple(tool.name for tool in PLANNING_TOOLS)
        assert evaluator_answer.offered == evaluator_call.offered
        # 两个 loop 的真实轨迹都来自实际完成的调用，且都是本次 Intent 的必需事实集。
        assert executed_tools(planner_answer) == GENERATE_FACT_TOOLS
        assert executed_tools(evaluator_answer) == GENERATE_FACT_TOOLS
        assert "user-1" not in planner_call.messages[0].content

    harnesses = build_plan_tool_harnesses(timeout_seconds=TOOL_TIMEOUT_SECONDS)
    assert "planning_tools" in harnesses.planning.get_graph().nodes
    assert "evaluation_tools" in harnesses.evaluation.get_graph().nodes
    assert "planning_tools" not in harnesses.evaluation.get_graph().nodes


async def test_adjust_plan_fast_fails_without_active_plan_and_calendar_facts(
    tmp_path: Path,
) -> None:
    """调整场景未真实调 active 计划与日历：必需事实缺项即明确失败，不写计划行。"""
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [{"domain": "plan_management", "action": "modify"}],
            **_plan_scripts(goal_alignment=[True]),
        },
        harness=_fact_loop(*GENERATE_FACT_TOOLS),
    ) as h:
        await _insert_active_plan(h.db, _bodyweight_plan_content())
        before = await _row_counts(h.db)

        with pytest.raises(MissingPlanFacts) as failure:
            await h.invoke("调整我的训练计划")

        assert "read_active_plan" in str(failure.value)
        assert "read_training_calendar" in str(failure.value)
        assert await _row_counts(h.db) == before


async def test_candidate_outside_the_real_search_results_fast_fails(
    tmp_path: Path,
) -> None:
    """真实 search_exercises 没返回候选动作：越界候选明确失败，不写计划行。"""
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [{"domain": "plan_management", "action": "create"}],
            **_plan_scripts(goal_alignment=[True]),
        },
        harness=_fact_loop(*GENERATE_FACT_TOOLS, query="zzzz"),
    ) as h:
        before = await _row_counts(h.db)

        with pytest.raises(UnregisteredCandidateExercise) as failure:
            await h.invoke("帮我检索一个不存在的动作并生成计划")

        assert "pull-up" in str(failure.value)
        assert await _row_counts(h.db) == before


async def test_adjust_plan_still_reads_the_active_plan(tmp_path: Path) -> None:
    """回归：调整计划仍以当前 active 为来源，新 draft 的 ``source_plan_id`` 等于该 active；

    调整的 ToolNode loop 真实调用 active 计划与训练日历两项追加事实。
    """
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [{"domain": "plan_management", "action": "modify"}],
            **_plan_scripts(goal_alignment=[True]),
        },
        harness=_fact_loops(2, *ADJUST_FACT_TOOLS),
    ) as h:
        active_id = await _insert_active_plan(h.db, _bodyweight_plan_content())
        result = await h.invoke("调整我的训练计划")

        assert result.intent == "adjust_plan"
        assert result.draft_plan_id is not None
        adjusted = await PlanRepo(h.db).read_by_id(result.draft_plan_id)
        assert adjusted is not None and adjusted.source_plan_id == active_id
        assert executed_tools(h.model.harness_calls[1]) == ADJUST_FACT_TOOLS


@pytest.mark.parametrize(
    "fields",
    [
        {"domain": "plan_management", "action": "query"},
        {"domain": "workout_execution", "action": "delete"},
        {"domain": "analytics", "action": "query", "knowledge_type": "methodology"},
    ],
)
async def test_illegal_router_output_aborts_before_any_branch(
    tmp_path: Path, fields: Mapping[str, Any]
) -> None:
    """非法组合（含已移除的 delete 取值）在进入任何分支前失败，不产生计划写入与可见消息。"""
    async with _harness(tmp_path, structured=_route(**fields)) as h:
        before = await _row_counts(h.db)

        with pytest.raises(ValidationError):
            await h.invoke("删除我的训练计划")

        assert await _row_counts(h.db) == before
        assert h.model.text_calls() == []


async def test_safety_hit_streams_only_the_safety_nodes_without_a_model_call(
    tmp_path: Path,
) -> None:
    """红旗词命中：节点序列只有 safety_scan 与 safety_stop，模型零调用、业务库零写入。"""
    async with _harness(tmp_path) as h:
        before = await _row_counts(h.db)
        events = await h.stream("训练时胸部异常不适，我还能继续吗？")

        assert [(event.event, event.data) for event in events] == [
            ("node", {"name": "safety_scan"}),
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
        assert h.model.calls == []
        assert h.model.harness_calls == []
        assert await _row_counts(h.db) == before


async def test_conversation_intent_emits_its_branch_node_before_any_visible_text(
    tmp_path: Path,
) -> None:
    """五项会话 Intent 的节点序列可观察：安全扫描、路由、General 依次先于可见文本。"""
    async with _harness(
        tmp_path,
        structured={
            FitnessIntent: [
                {
                    "domain": "workout_execution",
                    "action": "create",
                    "execution_type": "natural_language_record",
                }
            ],
            **_extraction_scripts(),
        },
        harness=[
            tool_call("prepare_workout_record", {"request": "记录今天做8个引体"}),
            final_answer(SUMMARY),
        ],
    ) as h:
        events = await h.stream("记录今天做8个引体")

        assert [event.event for event in events] == [
            "node",
            "node",
            "node",
            "message",
            "waiting",
            "done",
        ]
        assert [event.data["name"] for event in events[:3]] == [
            "safety_scan",
            "router_node",
            "natural_language_record",
        ]
        assert events[4].data == {
            "type": "workout_confirmation",
            "workout": {
                "performed_on": SCHEDULED_ON,
                "sets": [
                    {
                        "exercise_id": PULL_UP,
                        "set_no": 1,
                        "set_type": "work",
                        "load_convention": None,
                        "weight_kg": None,
                        "reps": 8,
                        "duration_seconds": None,
                    }
                ],
                "plan_session_id": None,
                "auto_link": True,
            },
            "candidate_plan_sessions": [],
        }
        assert events[5].data["intent"] == "natural_language_record"
