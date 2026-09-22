"""General 路径的只读 Tool Registry：五项会话 Intent 的白名单、两类打卡载荷与标准 ``ui_actions``。

白名单按 Intent 在装配期固化（:func:`build_general_tool_harnesses`），模型参数里不出现身份；两个
打卡 Tool 都只读：候选与等待载荷照旧交给确认端点写入，这里不碰业务训练表。专家库只作为 Skill 的
知识层装载，不取得 Tool 与写入权限。
"""

import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal

from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field

from app.application.agent.budget import request_structured_model
from app.application.agent.contracts import Intent, LoadedSkill, SkillBundle
from app.application.agent.harness.cache import ToolResultCache
from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.graph import build_tool_harness
from app.application.agent.harness.tools.training import (
    PROGRESS_TOOLS,
    SCHEDULE_TOOLS,
    HarnessToolArgs,
    TrainingHarnessContext,
    dump_tool_payload,
    read_active_plan,
    read_training_history,
    search_exercises,
)
from app.application.agent.prompts import (
    FORM_RECORD_GUIDE,
    NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
)
from app.application.agent.router import NON_PLAN_INTENTS
from app.application.ports import SkillSource
from app.domain.actions.schema import Exercise, LoadConvention
from app.domain.conversations.context import ContextMessage, history_payload
from app.domain.plans.schema import PlanSession
from app.domain.records.schema import SetType, WorkoutSetInput


class ExtractedWorkoutSet(BaseModel):
    """一组训练事实（模型提取结果）。"""

    model_config = ConfigDict(extra="forbid")

    exercise_id: str
    set_no: int
    set_type: SetType
    reps: int | None = None
    load_convention: LoadConvention | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


class ExtractedWorkout(BaseModel):
    """一次自然语言打卡的结构化提取结果：业务自然日 ＋ 全部组。"""

    model_config = ConfigDict(extra="forbid")

    performed_on: date
    sets: list[ExtractedWorkoutSet]


class WorkoutRecordFormArgs(HarnessToolArgs):
    """打卡表单：无模型参数。"""


class PrepareWorkoutRecordArgs(HarnessToolArgs):
    """自然语言打卡候选：只收本次训练描述原文，业务日与目录由运行上下文注入。"""

    request: str = Field(min_length=1)


#: ``workout_form`` 的字段清单：前端按它渲染，服务端字段校验仍由既有记录接口承担。
WORKOUT_FORM_FIELDS: tuple[str, ...] = (
    "performed_on",
    "exercise_id",
    "set_no",
    "set_type",
    "reps",
    "load_convention",
    "weight_kg",
    "duration_seconds",
    "plan_session_id",
)


def workout_form_payload() -> dict[str, Any]:
    """``workout_form`` 载荷：表单标识、字段清单与既有引导文案。"""
    return {
        "form": "workout_record",
        "fields": list(WORKOUT_FORM_FIELDS),
        "guide": FORM_RECORD_GUIDE,
    }


@tool(args_schema=WorkoutRecordFormArgs)
async def get_workout_record_form(
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取打卡表单的字段清单与提交引导；提交由既有记录接口完成，本 Tool 不写库。"""
    return dump_tool_payload(workout_form_payload())


async def prepare_workout_record_payload(
    context: TrainingHarnessContext,
    request: str,
    *,
    history: Sequence[ContextMessage] = (),
    skill: SkillBundle | None = None,
) -> dict[str, Any]:
    """自然语言描述 → 已校验的打卡候选：日期、组事实与当天未完成计划日程，不入库。

    提取用的模型请求与本次 Run 共用同一份预算；``history`` 由直接调用方给出，经 harness 驱动时
    历史已在消息序列里。
    """
    catalog = await context.catalog.list_all()
    extraction = await request_structured_model(
        context.model,
        NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
        {
            "request": request,
            **history_payload(history),
            "business_day": context.business_day.isoformat(),
            "actions": [_action_payload(exercise) for exercise in catalog],
            **({"skill": skill.model_dump()} if skill is not None else {}),
        },
        context.budget,
        ExtractedWorkout,
    )
    day, facts = await context.records.validate_record_facts(
        extraction.performed_on,
        tuple(_workout_set_input(item) for item in extraction.sets),
    )
    candidates = await context.records.list_unfinished_plan_sessions(day)
    return {
        "workout": _workout_payload(day, facts),
        "candidate_plan_sessions": [
            _candidate_plan_session_payload(session) for session in candidates
        ],
    }


@tool(args_schema=PrepareWorkoutRecordArgs)
async def prepare_workout_record(
    request: str,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """把一次训练描述提取为待确认的打卡候选；本 Tool 不写业务训练表，确认写入由确认端点承担。"""
    return dump_tool_payload(await prepare_workout_record_payload(runtime.context, request))


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

GENERAL_SKILL_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(name for names in GENERAL_INTENT_SKILLS.values() for name in names)
)

#: 五项会话 Intent 的标准 ``ui_actions`` 类型：前端按 ``type`` 分派渲染。
GeneralUiActionType = Literal[
    "workout_form",
    "workout_confirmation",
    "schedule_view",
    "progress_view",
]

#: 只读事实类的 Intent → 视图类型：无事实可给时不发 ``ui_actions``。
VIEW_ACTION_TYPES: Mapping[Intent, GeneralUiActionType] = {
    "view_schedule": "schedule_view",
    "view_progress": "progress_view",
}

def workout_form_action() -> dict[str, Any]:
    """``workout_form`` 动作：表单字段清单随动作下行，前端据此渲染打卡表单。"""
    return {"type": "workout_form", **workout_form_payload()}


def workout_confirmation_action(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``workout_confirmation`` 动作：待确认的训练事实与候选日程，确认前不写业务训练表。"""
    return {"type": "workout_confirmation", **payload}


def general_ui_actions(
    intent: Intent, messages: Sequence[BaseMessage]
) -> tuple[dict[str, Any], ...]:
    """harness 消息序列 → 标准 ``ui_actions``：只读事实按 Intent 的视图类型封装，报错的工具不入。"""
    action_type = VIEW_ACTION_TYPES.get(intent)
    if action_type is None:
        return ()
    facts = [
        {"tool": message.name, "result": json.loads(str(message.content))}
        for message in messages
        if isinstance(message, ToolMessage)
        and message.status == "success"
        and message.name is not None
    ]
    if not facts:
        return ()
    return ({"type": action_type, "facts": facts},)


def general_skill_bundle(skills: SkillSource, intent: Intent) -> SkillBundle:
    """按会话 Intent 装载模型指令与其明确引用的 reference。"""
    loaded: tuple[LoadedSkill, ...] = tuple(
        skills.load(name) for name in GENERAL_INTENT_SKILLS[intent]
    )
    return SkillBundle(
        names=tuple(item.metadata.name for item in loaded),
        system_instructions="\n\n".join(item.body for item in loaded),
        references=tuple(
            reference.text for item in loaded for reference in item.references
        ),
        version=" | ".join(item.metadata.description for item in loaded),
    )


def general_system_prompt(base: str, bundle: SkillBundle) -> str:
    """基础节点指令加本次 Intent 的 Skill 正文与 references。"""
    return "\n\n".join(
        part for part in (base, bundle.system_instructions, *bundle.references) if part
    )


def build_general_tool_harnesses(
    *, timeout_seconds: float, cache: ToolResultCache | None = None
) -> Mapping[Intent, CompiledStateGraph]:
    """按白名单固化五项会话 Intent 的 ``general_tools`` ToolNode：一 Intent 一图，不共享工具集合。"""
    return {
        intent: build_tool_harness(
            tools, timeout_seconds=timeout_seconds, cache=cache
        )
        for intent, tools in GENERAL_INTENT_TOOLS.items()
    }


def _workout_set_input(item: ExtractedWorkoutSet) -> WorkoutSetInput:
    """提取结果的一条组 → 写入侧的组事实。"""
    return WorkoutSetInput(
        exercise_id=item.exercise_id,
        set_no=item.set_no,
        reps=item.reps,
        set_type=item.set_type,
        load_convention=item.load_convention,
        weight_kg=item.weight_kg,
        duration_seconds=item.duration_seconds,
    )


def _action_payload(exercise: Exercise) -> dict[str, Any]:
    """可读动作目录的一行：模型据此把自然语言动作匹配为稳定 ``exercise_id``。"""
    return {
        "exercise_id": exercise.id,
        "standard_name_zh": exercise.standard_name_zh,
        "aliases": list(exercise.aliases),
        "equipment_variant": exercise.equipment_variant,
        "modes": list(exercise.modes),
        "record_type": exercise.record_type,
        "load_convention": exercise.load_convention,
    }


def _workout_payload(day: date, facts: Sequence[WorkoutSetInput]) -> dict[str, Any]:
    """``workout_confirmation.workout``（前端确认 UI 的编辑数据源）：日期、组事实与关联日程默认值。"""
    return {
        "performed_on": day.isoformat(),
        "sets": [
            {
                "exercise_id": fact.exercise_id,
                "set_no": fact.set_no,
                "set_type": fact.set_type,
                "load_convention": fact.load_convention,
                "weight_kg": fact.weight_kg,
                "reps": fact.reps,
                "duration_seconds": fact.duration_seconds,
            }
            for fact in facts
        ],
        "plan_session_id": None,
        "auto_link": True,
    }


def _candidate_plan_session_payload(session: PlanSession) -> dict[str, Any]:
    """``workout_confirmation.candidate_plan_sessions`` 的一行。"""
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "scheduled_on": session.scheduled_on.isoformat(),
    }
