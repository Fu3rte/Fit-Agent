from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field

from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.tools.common import (
    HarnessToolArgs,
    TrainingHarnessContext,
    dump_tool_payload,
)


class ReadTrainingCalendarArgs(HarnessToolArgs):
    """月历参数：年份与月份的范围都由 Schema 边界表达。"""

    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


@tool(args_schema=ReadTrainingCalendarArgs)
async def read_training_calendar(
    year: int,
    month: int,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取一个自然月的计划日程状态与实际训练事实（只读当前 active 计划的日程）。"""
    calendar = await runtime.context.stats.calendar_month(year, month)
    return dump_tool_payload(calendar)
