from datetime import date, timedelta
from typing import Literal

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field

from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.tools.common import (
    HarnessToolArgs,
    StrictModel,
    TrainingHarnessContext,
    dump_tool_payload,
)
from app.domain.stats.schema import WorkoutGap


class ReadProgressArgs(HarnessToolArgs):
    """进展参数：统计窗口只约束体重变化，PB 与停训天数保持各自既有口径。"""

    window_days: int = Field(default=30, ge=7, le=365)


class PersonalBestView(StrictModel):
    """一条 PB 的稳定投影：来源组身份与适用重量／负重口径。"""

    exercise_id: str
    exercise_name: str
    pb_type: Literal["weight_pb", "reps_pb", "duration_pb"]
    value: float
    workout_session_id: int
    set_no: int
    performed_on: date
    load_convention: str | None = None
    weight_kg: float | None = None


class WeightChangeView(StrictModel):
    """窗口内体重变化：无记录为 ``no_data``，不足两条为 ``insufficient_data``。"""

    status: Literal["ok", "no_data", "insufficient_data"]
    current: float | None
    current_on: date | None
    previous: float | None
    previous_on: date | None
    change: float | None


class InactivityView(StrictModel):
    """截至业务日的停训天数：没有训练历史即 ``no_data``。"""

    status: Literal["ok", "no_data"]
    days: int | None
    last_performed_on: date | None


class ProgressPayload(StrictModel):
    """进展摘要：业务日、统计窗口、全时 PB、窗口体重变化与停训天数。"""

    business_day: date
    window_days: int
    personal_bests: tuple[PersonalBestView, ...]
    weight_change: WeightChangeView
    days_since_last_workout: InactivityView


@tool(args_schema=ReadProgressArgs)
async def read_progress(
    window_days: int,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取全时三类 PB、窗口内体重变化与截至业务日的停训天数。

    ``window_days`` 只界定体重变化的闭区间 ``[business_day - (window_days - 1), business_day]``；
    PB 与停训天数分别保持全时口径与业务日口径。所有数值来自确定性统计入口，模型只解释结果。
    """
    context = runtime.context
    business_day = context.business_day
    weight_change = await context.stats.weight_change_between(
        business_day - timedelta(days=window_days - 1), business_day
    )
    personal_bests = await context.stats.list_personal_bests()
    inactivity = await context.stats.days_since_last_workout(business_day)
    return dump_tool_payload(
        ProgressPayload(
            business_day=business_day,
            window_days=window_days,
            personal_bests=tuple(
                PersonalBestView.model_validate(best, from_attributes=True)
                for best in personal_bests
            ),
            weight_change=WeightChangeView.model_validate(
                weight_change, from_attributes=True
            ),
            days_since_last_workout=_inactivity_view(inactivity),
        )
    )


def _inactivity_view(gap: WorkoutGap) -> InactivityView:
    """领域 ``WorkoutGap`` 只有 ``ok``／``no_data``；其余值即统计口径损坏，就地失败。"""
    if gap.status not in ("ok", "no_data"):
        raise ValueError(f"停训状态不在允许集合内：{gap.status}")
    return InactivityView(
        status=gap.status,
        days=gap.days,
        last_performed_on=gap.last_performed_on,
    )
