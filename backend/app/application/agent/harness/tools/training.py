import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi.encoders import jsonable_encoder
from langchain_core.tools import InjectedToolArg, tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.harness.declaration import HarnessContext, HarnessState
from app.application.ports import ExerciseCatalog, Plans, WorkoutRecords
from app.application.services.stats_service import StatsService
from app.domain.plans.schema import PLAN_WINDOW_DAYS, PlanDraft


@dataclass(frozen=True, slots=True)
class TrainingHarnessContext(HarnessContext):
    """业务工具上下文：注入的业务日期、共享请求预算与既有只读入口。

    ``budget`` 收窄为 :class:`ModelRequestBudget`：业务工具内的模型请求（自然语言提取）与 agent loop
    的模型请求共用同一份次数与时限。
    """

    budget: ModelRequestBudget
    business_day: date
    plans: Plans
    catalog: ExerciseCatalog
    records: WorkoutRecords
    stats: StatsService


class HarnessToolArgs(BaseModel):
    """Harness 工具参数基线：未声明字段一律拒绝，并声明 ToolNode 注入的上下文参数。

    langchain-core 1.6.3 的 ``BaseTool._parse_input`` 用 ``args_schema`` 校验注入后的参数，
    ``runtime`` 必须声明为字段才通得过校验；它带 ``InjectedToolArg``，因此不出现在模型可见的
    ``tool_call_schema`` 里（``convert_to_openai_tool`` 也就看不到它）。
    """

    model_config = ConfigDict(extra="forbid")

    runtime: Annotated[Any, InjectedToolArg]


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


@tool(args_schema=ReadActivePlanArgs)
async def read_active_plan(
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取当前 active 计划的目标、每周次数、训练日与七天覆盖范围；没有 active 计划时为 null。"""
    context = runtime.context
    payload: dict[str, Any] = {"business_day": context.business_day.isoformat()}
    active = await context.plans.read_active()
    if active is None:
        payload["active_plan"] = None
        return dump_tool_payload(payload)
    draft = PlanDraft.model_validate(active.structured_content)
    names = {
        exercise.id: exercise.standard_name_zh
        for exercise in await context.catalog.list_all()
    }
    content = draft.model_dump(mode="json")
    payload["active_plan"] = {
        "id": active.id,
        "version": active.version,
        "coverage": _coverage(draft.starts_on),
        "goal": draft.goal,
        "weekly_frequency": draft.weekly_frequency,
        "training_days": _named_training_days(content["training_days"], names),
    }
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
    """按标准名、别名与器械变体检索动作目录，只回稳定 ``exercise_id`` 与记录口径。"""
    needle = query.casefold()
    catalog = await runtime.context.catalog.list_all()
    return dump_tool_payload(
        [
            {
                "exercise_id": exercise.id,
                "standard_name_zh": exercise.standard_name_zh,
                "aliases": list(exercise.aliases),
                "equipment_variant": exercise.equipment_variant,
                "record_type": exercise.record_type,
                "load_convention": exercise.load_convention,
            }
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
    )


SCHEDULE_TOOLS = (
    read_active_plan,
    read_training_calendar,
)

PROGRESS_TOOLS = (
    read_progress,
    read_training_history,
)


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
