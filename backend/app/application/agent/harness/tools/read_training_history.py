from datetime import date
from typing import Literal

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field, model_validator

from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.tools.common import (
    HarnessToolArgs,
    StrictModel,
    TrainingHarnessContext,
    dump_tool_payload,
)


class ReadTrainingHistoryArgs(HarnessToolArgs):
    """训练历史参数：日期闭区间、动作集合与数量条件取 AND，缺省取最近 20 次。"""

    from_on: date | None = None
    to_on: date | None = None
    exercise_ids: tuple[str, ...] = Field(default=(), max_length=20)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def _require_ordered_window(self) -> "ReadTrainingHistoryArgs":
        if (
            self.from_on is not None
            and self.to_on is not None
            and self.to_on < self.from_on
        ):
            raise ValueError("to_on 必须晚于或等于 from_on")
        return self


class WorkoutSetView(StrictModel):
    """一组训练事实：组类型、负重口径与记录口径要求的度量值。"""

    exercise_id: str
    set_no: int
    set_type: Literal["warmup", "work", "assisted"]
    load_convention: str | None
    weight_kg: float | None
    reps: int | None
    duration_seconds: int | None


class WorkoutSessionView(StrictModel):
    """一次训练：身份、发生日期、关联日程与全部组事实。"""

    id: int
    performed_on: date
    plan_session_id: int | None
    sets: tuple[WorkoutSetView, ...]


class TrainingHistoryPayload(StrictModel):
    """训练历史：命中的训练会话（最新在前），无记录即空 tuple。"""

    sessions: tuple[WorkoutSessionView, ...]


@tool(args_schema=ReadTrainingHistoryArgs)
async def read_training_history(
    from_on: date | None,
    to_on: date | None,
    exercise_ids: tuple[str, ...],
    limit: int,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """按日期闭区间、动作集合与数量条件回溯训练记录。

    三个条件取 AND：``exercise_ids`` 为空时不限制动作；命中会话返回其全部组事实。
    结果按 ``performed_on`` 降序、身份降序排列，``limit`` 在过滤与排序之后截断。
    """
    sessions = await runtime.context.records.list_recent(
        limit, from_on=from_on, to_on=to_on, exercise_ids=exercise_ids
    )
    return dump_tool_payload(
        TrainingHistoryPayload(
            sessions=tuple(
                WorkoutSessionView.model_validate(session, from_attributes=True)
                for session in sessions
            )
        )
    )
