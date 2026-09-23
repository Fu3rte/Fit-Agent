from collections.abc import Mapping
from datetime import date, timedelta

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field

from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.tools.common import (
    CatalogReferentialIntegrityError,
    HarnessToolArgs,
    StrictModel,
    TrainingHarnessContext,
    dump_tool_payload,
)
from app.domain.plans.schema import PLAN_WINDOW_DAYS, PlanDraft


class ReadActivePlanArgs(HarnessToolArgs):
    """active 计划：无模型参数。"""


class ActivePlanExercise(StrictModel):
    """计划里的一个动作：canonical 身份、目录名称、组数与处方序列化投影。"""

    exercise_id: str
    exercise_name: str
    sets: int = Field(ge=1)
    prescription: dict[str, object]


class ActivePlanDay(StrictModel):
    """一个训练日：业务日期与当天的动作序列。"""

    scheduled_on: date
    exercises: tuple[ActivePlanExercise, ...]


class ActivePlanView(StrictModel):
    """当前 active 计划的稳定投影：身份、版本、七天窗口与训练日。"""

    id: int = Field(ge=1)
    version: int = Field(ge=1)
    starts_on: date
    ends_on: date
    goal: str
    weekly_frequency: int = Field(ge=1, le=7)
    training_days: tuple[ActivePlanDay, ...]


class ReadActivePlanPayload(StrictModel):
    """读取结果：业务日与 active 计划；没有 active 计划时为 null。"""

    business_day: date
    active_plan: ActivePlanView | None


@tool(args_schema=ReadActivePlanArgs)
async def read_active_plan(
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取当前 active 计划与其七天窗口内的训练日；目录名称按 canonical 身份关联。

    没有 active 计划时 ``active_plan`` 为 null。计划引用的 canonical 动作缺失或名称无法解析即
    明确失败，不用 null 名称掩盖目录引用损坏。
    """
    context = runtime.context
    active = await context.plans.read_active()
    if active is None:
        return dump_tool_payload(
            ReadActivePlanPayload(
                business_day=context.business_day, active_plan=None
            )
        )
    draft = PlanDraft.model_validate(active.structured_content)
    names = {
        exercise.id: exercise.standard_name_zh
        for exercise in await context.catalog.list_all()
    }
    payload = ReadActivePlanPayload(
        business_day=context.business_day,
        active_plan=ActivePlanView(
            id=active.id,
            version=active.version,
            starts_on=draft.starts_on,
            ends_on=draft.starts_on + timedelta(days=PLAN_WINDOW_DAYS - 1),
            goal=draft.goal,
            weekly_frequency=draft.weekly_frequency,
            training_days=tuple(
                ActivePlanDay(
                    scheduled_on=day.scheduled_on,
                    exercises=tuple(
                        ActivePlanExercise(
                            exercise_id=planned.exercise_id,
                            exercise_name=_require_name(names, planned.exercise_id),
                            sets=planned.sets,
                            prescription=planned.prescription.model_dump(mode="json"),
                        )
                        for planned in day.exercises
                    ),
                )
                for day in sorted(
                    draft.training_days, key=lambda day: day.scheduled_on
                )
            ),
        ),
    )
    return dump_tool_payload(payload)


def _require_name(names: Mapping[str, str], exercise_id: str) -> str:
    """canonical 动作名称：目录缺失或名称为空即目录引用损坏，就地崩溃。"""
    name = names.get(exercise_id)
    if not name:
        raise CatalogReferentialIntegrityError(
            f"active 计划引用的 canonical 动作缺失或名称无法解析：{exercise_id}"
        )
    return name
