import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi.encoders import jsonable_encoder
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.tools import InjectedToolArg, tool
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.contracts import (
    PlanToolHarnesses,
    ToolEvidence,
    ToolExecutionContext,
)
from app.application.agent.harness.cache import ToolResultCache
from app.application.agent.harness.declaration import HarnessContext, HarnessState
from app.application.agent.harness.graph import build_tool_harness
from app.application.ports import (
    ExerciseCatalog,
    Plans,
    ProfileReads,
    WorkoutRecords,
)
from app.application.services.stats_service import StatsService
from app.domain.actions.schema import Exercise
from app.domain.plans.rules import (
    active_load_targets,
    resolve_progression,
    resolve_starting_load,
)
from app.domain.plans.schema import PLAN_WINDOW_DAYS, PlanDraft


@dataclass(frozen=True, slots=True)
class TrainingHarnessContext(HarnessContext):
    """业务工具上下文：注入的业务日期、共享请求预算与既有只读入口。

    ``budget`` 收窄为 :class:`ModelRequestBudget`：业务工具内的模型请求（自然语言提取）与 agent loop
    的模型请求共用同一份次数与时限。
    """

    budget: ModelRequestBudget
    business_day: date
    profiles: ProfileReads
    plans: Plans
    catalog: ExerciseCatalog
    records: WorkoutRecords
    stats: StatsService


@dataclass(frozen=True, slots=True)
class PlanToolHarnessContext(TrainingHarnessContext):
    """计划路径 ToolNode 的运行上下文：业务只读入口 ＋ 本次 Run 的事实快照键。

    ``snapshot`` 只由 Runtime 注入到 ToolNode 边界；模型可见参数里不出现身份、Run 与 revision 字段。
    两个 ToolNode 各自持有一份上下文，工具调用预算彼此不共享。
    """

    snapshot: ToolExecutionContext


#: 计划路径两个 ToolNode 的节点名：Planner 与 Evaluator 各一份独立节点。
PLANNING_TOOLS_NODE_NAME = "planning_tools"
EVALUATION_TOOLS_NODE_NAME = "evaluation_tools"


class HarnessToolArgs(BaseModel):
    """Harness 工具参数基线：未声明字段一律拒绝，并声明 ToolNode 注入的上下文参数。

    langchain-core 1.6.3 的 ``BaseTool._parse_input`` 用 ``args_schema`` 校验注入后的参数，
    ``runtime`` 必须声明为字段才通得过校验；它带 ``InjectedToolArg``，因此不出现在模型可见的
    ``tool_call_schema`` 里（``convert_to_openai_tool`` 也就看不到它）。
    """

    model_config = ConfigDict(extra="forbid")

    runtime: Annotated[Any, InjectedToolArg]


class ReadUserProfileArgs(HarnessToolArgs):
    """画像事实：无模型参数。"""


class ReadActivePlanArgs(HarnessToolArgs):
    """active 计划：无模型参数。"""


class ReadTrainingCalendarArgs(HarnessToolArgs):
    """月历参数：年份与月份的范围都由 Schema 边界表达。"""

    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class ReadTrainingHistoryArgs(HarnessToolArgs):
    """训练历史参数：次数上下界由 Schema 表达，缺省取最近 4 次。"""

    limit: int = Field(default=4, ge=1, le=20)


class ReadProgressArgs(HarnessToolArgs):
    """进展：无模型参数。"""


class SearchExercisesArgs(HarnessToolArgs):
    """目录检索参数：查询词非空，只做子串匹配。"""

    query: str = Field(min_length=1)


@tool(args_schema=ReadUserProfileArgs)
async def read_user_profile(
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取当前画像的训练事实与安全约束；未建档为 null，模型只解释这里的值。"""
    profile = await runtime.context.profiles.read()
    if profile is None:
        return dump_tool_payload({"profile": None})
    payload = jsonable_encoder(profile)
    return dump_tool_payload({"profile": payload})


@tool(args_schema=ReadActivePlanArgs)
async def read_active_plan(
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取当前 active 计划的目标、每周次数、训练日与七天覆盖范围，并给出负重目标的渐进决策。

    没有 active 计划时 ``active_plan`` 为 null、``progression_decisions`` 为空列表。
    """
    context = runtime.context
    payload: dict[str, Any] = {"business_day": context.business_day.isoformat()}
    active = await context.plans.read_active()
    if active is None:
        payload["active_plan"] = None
        payload["progression_decisions"] = []
        return dump_tool_payload(payload)
    draft = PlanDraft.model_validate(active.structured_content)
    exercises = {
        exercise.id: exercise for exercise in await context.catalog.list_all()
    }
    content = draft.model_dump(mode="json")
    payload["active_plan"] = {
        "id": active.id,
        "version": active.version,
        "coverage": _coverage(draft.starts_on),
        "goal": draft.goal,
        "explanation": draft.explanation,
        "weekly_frequency": draft.weekly_frequency,
        "training_days": _named_training_days(
            content["training_days"],
            {
                exercise_id: exercise.standard_name_zh
                for exercise_id, exercise in exercises.items()
            },
        ),
    }
    payload["progression_decisions"] = await _progression_decisions(
        context, plan_id=active.id, active_draft=draft, exercises=exercises
    )
    return dump_tool_payload(payload)


@tool(args_schema=ReadTrainingCalendarArgs)
async def read_training_calendar(
    year: int,
    month: int,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取一个自然月的计划日程状态与实际训练事实（只读当前 active 计划的日程）。"""
    calendar = await runtime.context.stats.calendar_month(year, month)
    return dump_tool_payload(calendar)


@tool(args_schema=ReadTrainingHistoryArgs)
async def read_training_history(
    limit: int,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取最近若干次训练的身份、日期、关联计划日程与已有训练组事实。"""
    sessions = await runtime.context.records.list_recent(limit)
    return dump_tool_payload(sessions)


@tool(args_schema=ReadProgressArgs)
async def read_progress(
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取三类 PB 与确定性趋势摘要；统计值来自既有实现，模型只负责解释。"""
    context = runtime.context
    return dump_tool_payload(
        {
            "business_day": context.business_day.isoformat(),
            "personal_bests": await context.stats.list_personal_bests(),
            "trend_summary": await context.stats.trend_summary(context.business_day),
        }
    )


@tool(args_schema=SearchExercisesArgs)
async def search_exercises(
    query: str,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """按标准名、别名与器械变体检索动作目录：回稳定 ``exercise_id``、记录口径、是否可推荐与增重单位，

    命中动作同时附上该动作最近一次有效工作组的 ``starting_load``（无历史即待校准）。
    """
    needle = query.casefold()
    catalog = await runtime.context.catalog.list_all()
    matched = [
        exercise
        for exercise in catalog
        if needle
        in " ".join(
            (
                exercise.standard_name_zh,
                exercise.equipment_variant,
                *exercise.aliases,
            )
        ).casefold()
    ]
    work_sets = await runtime.context.stats.list_valid_work_sets() if matched else ()
    return dump_tool_payload(
        [
            {
                "exercise_id": exercise.id,
                "standard_name_zh": exercise.standard_name_zh,
                "aliases": list(exercise.aliases),
                "equipment_variant": exercise.equipment_variant,
                "record_type": exercise.record_type,
                "load_convention": exercise.load_convention,
                "recommendable": exercise.recommendable,
                "min_load_increment_kg": exercise.min_load_increment_kg,
                "starting_load": resolve_starting_load(
                    work_sets, exercise_id=exercise.id
                ).model_dump(mode="json"),
            }
            for exercise in matched
        ]
    )


SCHEDULE_TOOLS = (
    read_active_plan,
    read_training_calendar,
)

PROGRESS_TOOLS = (
    read_progress,
    read_training_history,
)

PROFILE_TOOLS = (read_user_profile,)

#: 计划路径两个 LLM Node 各自的白名单：同一只读 Registry，两个独立 ToolNode。
PLANNING_TOOLS = (
    read_user_profile,
    read_active_plan,
    read_training_calendar,
    read_training_history,
    read_progress,
    search_exercises,
)

#: Evaluator 的独立白名单对象：与 Planner 同集不同元组，预算与调用轨迹各自独立。
EVALUATION_TOOLS = (*PLANNING_TOOLS,)

#: 每个计划 Intent 在模型出候选前必须读到的事实；缺项即明确失败，不由模型自行省略。
PLAN_REQUIRED_FACTS: dict[str, tuple[str, ...]] = {
    "generate_plan": (
        "read_user_profile",
        "read_training_history",
        "read_progress",
        "search_exercises",
    ),
    "adjust_plan": (
        "read_user_profile",
        "read_training_history",
        "read_progress",
        "search_exercises",
        "read_active_plan",
        "read_training_calendar",
    ),
}

#: 工具名 → 它读取的事实域 revision：每条 ToolEvidence 都出自这张表。
PLAN_TOOL_REVISION_DOMAINS: dict[str, tuple[str, ...]] = {
    "read_user_profile": ("profile",),
    "read_training_history": ("workouts",),
    "read_progress": ("workouts",),
    "search_exercises": ("catalog", "workouts"),
    "read_active_plan": ("plans", "catalog", "workouts"),
    "read_training_calendar": ("plans", "workouts"),
}

#: ToolExecutionContext 的四个 revision 域；计划路径读候选前要求全部在位。
PLAN_REVISION_DOMAINS: tuple[str, ...] = ("profile", "workouts", "plans", "catalog")

#: 目录域 revision 由 ``PRAGMA user_version`` 承担，不读 tool_cache_revisions。
CATALOG_REVISION_DOMAIN: str = "catalog"

#: 事实域 → ToolExecutionContext 上的 revision 字段。
TOOL_CONTEXT_REVISION_FIELDS: dict[str, str] = {
    "profile": "profile_revision",
    "workouts": "workouts_revision",
    "plans": "plans_revision",
    "catalog": "catalog_revision",
}


class MissingPlanFacts(ValueError):
    """计划 Intent 的必需事实未全部读到：明确失败，不把缺项的候选交给评审。"""


class UnregisteredCandidateExercise(ValueError):
    """候选计划引用了本次 ``search_exercises`` 没有返回的 ``exercise_id``。"""


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """一次真实完成的只读 Tool 调用：工具名与它返回给模型的载荷。"""

    tool_name: str
    payload: Any


def build_plan_tool_harnesses(
    *, timeout_seconds: float, cache: ToolResultCache | None = None
) -> PlanToolHarnesses:
    """装配计划路径的两个 ToolNode：同一只读 Registry，白名单元组、节点名与调用轨迹各自独立。"""
    return PlanToolHarnesses(
        planning=build_tool_harness(
            PLANNING_TOOLS,
            timeout_seconds=timeout_seconds,
            cache=cache,
            tools_node_name=PLANNING_TOOLS_NODE_NAME,
        ),
        evaluation=build_tool_harness(
            EVALUATION_TOOLS,
            timeout_seconds=timeout_seconds,
            cache=cache,
            tools_node_name=EVALUATION_TOOLS_NODE_NAME,
        ),
    )


async def run_plan_fact_loop(
    harness: CompiledStateGraph,
    *,
    context: PlanToolHarnessContext,
    messages: Sequence[BaseMessage],
) -> tuple[ToolCallRecord, ...]:
    """执行一次 ToolNode loop 并返回真实完成的调用轨迹；调用哪些工具由模型自选。"""
    result = await harness.ainvoke({"messages": list(messages)}, context=context)
    return tool_call_records(result["messages"])


def tool_call_records(
    messages: Sequence[BaseMessage],
) -> tuple[ToolCallRecord, ...]:
    """harness 消息序列 → 真实完成的工具调用轨迹：只含 ToolNode 已成功执行的调用。"""
    return tuple(
        ToolCallRecord(tool_name=message.name, payload=_tool_payload(message))
        for message in messages
        if isinstance(message, ToolMessage)
        and message.status == "success"
        and message.name is not None
    )


def _tool_payload(message: ToolMessage) -> Any:
    """ToolMessage 的载荷：工具输出本来就是 :func:`dump_tool_payload` 写出的 JSON 文本。"""
    try:
        return json.loads(message.content)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"工具 {message.name} 的输出不是合法 JSON：{message.content[:120]!r}"
        ) from error


def plan_fact_evidence(
    intent: str | None,
    *,
    snapshot: ToolExecutionContext,
    tool_calls: Sequence[ToolCallRecord],
) -> tuple[ToolEvidence, ...]:
    """候选的事实证据：必需事实按真实调用轨迹校验，每条证据带读取时的事实域 revision。"""
    required = PLAN_REQUIRED_FACTS.get(intent or "")
    if required is None:
        raise ValueError(f"未登记的计划 Intent：{intent!r}")
    called = [record.tool_name for record in tool_calls]
    missing = [name for name in required if name not in set(called)]
    if missing:
        raise MissingPlanFacts(
            f"{intent} 缺少必需事实，候选不得交给评审：{missing}"
        )
    return tuple(
        ToolEvidence(
            tool_name=name,
            revision_domain=domain,
            revision=getattr(snapshot, TOOL_CONTEXT_REVISION_FIELDS[domain]),
        )
        for name in dict.fromkeys(called)
        for domain in PLAN_TOOL_REVISION_DOMAINS[name]
    )


def canonical_exercise_ids(tool_calls: Sequence[ToolCallRecord]) -> frozenset[str]:
    """本次真实 ``search_exercises`` 调用返回的 canonical ``exercise_id`` 集合。"""
    return frozenset(
        item["exercise_id"]
        for record in tool_calls
        if record.tool_name == "search_exercises"
        for item in record.payload
    )


def require_canonical_candidates(
    draft: PlanDraft, *, tool_calls: Sequence[ToolCallRecord]
) -> None:
    """候选动作只允许真实 ``search_exercises`` 返回的 canonical id；越界即明确失败。"""
    canonical = canonical_exercise_ids(tool_calls)
    planned = dict.fromkeys(
        planned.exercise_id
        for day in draft.training_days
        for planned in day.exercises
    )
    outside = [exercise_id for exercise_id in planned if exercise_id not in canonical]
    if outside:
        raise UnregisteredCandidateExercise(
            f"候选计划引用了本次 search_exercises 未返回的动作：{outside}"
        )


async def _progression_decisions(
    context: TrainingHarnessContext,
    *,
    plan_id: int,
    active_draft: PlanDraft,
    exercises: Mapping[str, Exercise],
) -> list[dict[str, Any]]:
    """active 里有目标处方且目录给出增重单位的动作的渐进决策；关联训练由该计划的日程身份界定。"""
    targets = [
        (exercise_id, target, exercise.min_load_increment_kg)
        for exercise_id, target in active_load_targets(active_draft).items()
        if (exercise := exercises.get(exercise_id)) is not None
        and exercise.min_load_increment_kg is not None
    ]
    if not targets:
        return []
    session_ids = {session.id for session in await context.plans.list_sessions(plan_id)}
    linked = tuple(
        fact.workout_session_id
        for fact in await context.stats.list_linked_workouts()
        if fact.plan_session_id in session_ids
    )
    work_sets = await context.stats.list_valid_work_sets()
    return [
        {
            "exercise_id": exercise_id,
            **asdict(target),
            "decision": asdict(
                resolve_progression(
                    work_sets,
                    linked_workout_session_ids=linked,
                    target_sets=target.sets,
                    reps_min=target.reps_min,
                    reps_max=target.reps_max,
                    target_load_kg=target.target_load_kg,
                    increment_kg=increment_kg,
                )
            ),
        }
        for exercise_id, target, increment_kg in targets
    ]


def _coverage(starts_on: date) -> dict[str, str]:
    """计划窗口两端（含两端）：开始日与七天窗口末日。"""
    return {
        "starts_on": starts_on.isoformat(),
        "ends_on": (starts_on + timedelta(days=PLAN_WINDOW_DAYS - 1)).isoformat(),
    }


def _named_training_days(
    days: Sequence[Mapping[str, Any]], names: Mapping[str, str]
) -> list[dict[str, Any]]:
    """计划日按原字段展开，动作补上目录名称；目录缺失时名称为 null，不编造名称。"""
    return [
        {
            **day,
            "exercises": [
                {**exercise, "exercise_name": names.get(exercise["exercise_id"])}
                for exercise in day["exercises"]
            ],
        }
        for day in days
    ]


def dump_tool_payload(payload: Any) -> str:
    """工具输出文本：jsonable_encoder 归一化领域对象后交给标准 json.dumps。"""
    return json.dumps(jsonable_encoder(payload), ensure_ascii=False)
