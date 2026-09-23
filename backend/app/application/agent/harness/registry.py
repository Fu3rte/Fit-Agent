from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from app.application.agent.contracts import Intent, PlanToolHarnesses, SkillMetadata
from app.application.agent.harness.cache import ToolResultCache
from app.application.agent.harness.graph import build_tool_harness
from app.application.agent.harness.tools import (
    get_workout_record_form,
    prepare_workout_record,
    read_active_plan,
    read_progress,
    read_training_calendar,
    read_training_history,
    read_user_profile,
    search_exercises,
)
from app.application.agent.harness.tools.common import (
    ToolCallRecord,
    TrainingHarnessContext,
    tool_call_records,
)
from app.application.agent.harness.tools.read_skill import skill_reading_tools
from app.application.agent.router import NON_PLAN_INTENTS
from app.application.ports import SkillSource
from app.domain.plans.schema import PlanDraft

#: 计划路径两个 ToolNode 的节点名：Planner 与 Evaluator 各一份独立节点。
PLANNING_TOOLS_NODE_NAME = "planning_tools"
EVALUATION_TOOLS_NODE_NAME = "evaluation_tools"

SCHEDULE_TOOLS = (
    read_active_plan,
    read_training_calendar,
)

PROGRESS_TOOLS = (
    read_progress,
    read_training_history,
)

#: 计划路径两个 LLM Node 各自的白名单：同一只读 Registry，两个独立 ToolNode。
PLANNING_TOOLS = (
    read_user_profile,
    read_active_plan,
    read_training_calendar,
    read_training_history,
    read_progress,
    search_exercises,
)

#: 五项会话 Intent → 只读 Tool 白名单：Intent 之外的工具未挂载，模型看不到也未绑定。
GENERAL_INTENT_TOOLS: Mapping[Intent, tuple[BaseTool, ...]] = {
    "form_record": (get_workout_record_form,),
    "natural_language_record": (prepare_workout_record, search_exercises),
    "view_schedule": SCHEDULE_TOOLS,
    "view_progress": PROGRESS_TOOLS,
    "general": (
        search_exercises,
        read_training_history,
        read_active_plan,
        read_user_profile,
    ),
}

if set(GENERAL_INTENT_TOOLS) != set(NON_PLAN_INTENTS):
    raise ValueError("General 白名单表与五项会话 Intent 集合不一致")

#: 会话 Intent 只装载本次需要的 Skill；空 tuple 表示该路径只需 Tool Contract。
GENERAL_INTENT_SKILLS: Mapping[Intent, tuple[str, ...]] = {
    "form_record": (),
    "natural_language_record": ("workout-logging",),
    "view_schedule": (),
    "view_progress": ("strength-training",),
    "general": ("fitness-knowledge", "exercise-guidance", "strength-training"),
}

if set(GENERAL_INTENT_SKILLS) != set(NON_PLAN_INTENTS):
    raise ValueError("General Skill 表与五项会话 Intent 集合不一致")


def general_ui_actions(
    intent: Intent, messages: Sequence[BaseMessage]
) -> tuple[dict[str, Any], ...]:
    """harness 消息序列 → 标准 ``ui_actions``：只读事实按 Intent 的视图类型封装，报错的工具不入。"""
    action_type = {
        "view_schedule": "schedule_view",
        "view_progress": "progress_view",
    }.get(intent)
    if action_type is None:
        return ()
    facts = [
        {"tool": record.tool_name, "result": record.payload}
        for record in tool_call_records(messages)
    ]
    if not facts:
        return ()
    return ({"type": action_type, "facts": facts},)


def general_skill_metadata(
    skills: SkillSource, intent: Intent
) -> tuple[SkillMetadata, ...]:
    """返回本次 Intent 白名单内的 Skill 元数据。"""
    scanned = {item.name: item for item in skills.list_metadata()}
    return tuple(scanned[name] for name in GENERAL_INTENT_SKILLS[intent])


def general_system_prompt(base: str, allowed: tuple[SkillMetadata, ...]) -> str:
    """提示当前 Intent 可用 Skill 元数据与按需读取方式，不包含正文。"""
    if not allowed:
        return base
    listing = "\n".join(f"- {item.name}: {item.description}" for item in allowed)
    return (
        f"{base}\n\n可用 Skill（仅当前 Intent 白名单）：\n{listing}\n"
        "任务匹配时调用 read_skill(skill_name) 读取 SKILL.md；正文引用的 references/*.md "
        "仅在任务需要时调用 read_skill_reference(skill_name, relative_path) 单独读取。"
    )


def build_general_tool_harnesses(
    *,
    skills: SkillSource,
    timeout_seconds: float,
    cache: ToolResultCache | None = None,
) -> Mapping[Intent, CompiledStateGraph]:
    """按 Intent 固化业务 Tool 与 Skill 读取工具；读取工具仅挂载于有 Skill 权限的分支。"""
    metadata = {item.name: item for item in skills.list_metadata()}
    harnesses: dict[Intent, CompiledStateGraph] = {}
    for intent, tools in GENERAL_INTENT_TOOLS.items():
        names = GENERAL_INTENT_SKILLS[intent]
        allowed = tuple(metadata[name] for name in names)
        offered = (*tools, *skill_reading_tools(skills, allowed)) if allowed else tools
        harnesses[intent] = build_tool_harness(
            offered, timeout_seconds=timeout_seconds, cache=cache
        )
    return harnesses


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
            PLANNING_TOOLS,
            timeout_seconds=timeout_seconds,
            cache=cache,
            tools_node_name=EVALUATION_TOOLS_NODE_NAME,
        ),
    )


async def run_plan_fact_loop(
    harness: CompiledStateGraph,
    *,
    context: TrainingHarnessContext,
    messages: Sequence[BaseMessage],
) -> tuple[ToolCallRecord, ...]:
    """执行一次 ToolNode loop 并返回真实完成的调用轨迹；调用哪些工具由模型自选。"""
    result = await harness.ainvoke({"messages": list(messages)}, context=context)
    return tool_call_records(result["messages"])


class UnregisteredCandidateExercise(ValueError):
    """候选计划引用了本次 ``search_exercises`` 没有返回的 ``exercise_id``。"""


def require_canonical_candidates(
    draft: PlanDraft, *, tool_calls: Sequence[ToolCallRecord]
) -> None:
    """候选动作只允许真实 ``search_exercises`` 返回的 canonical id；越界即明确失败。"""
    canonical = frozenset(
        item["exercise_id"]
        for record in tool_calls
        if record.tool_name == "search_exercises"
        for item in record.payload["exercises"]
    )
    planned = dict.fromkeys(
        planned.exercise_id for day in draft.training_days for planned in day.exercises
    )
    outside = [exercise_id for exercise_id in planned if exercise_id not in canonical]
    if outside:
        raise UnregisteredCandidateExercise(
            f"候选计划引用了本次 search_exercises 未返回的动作：{outside}"
        )
