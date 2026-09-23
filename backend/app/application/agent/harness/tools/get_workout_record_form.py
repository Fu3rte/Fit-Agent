from typing import Any, Literal

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime

from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.tools.common import (
    WORKOUT_FORM_FIELDS,
    HarnessToolArgs,
    StrictModel,
    TrainingHarnessContext,
    dump_tool_payload,
)
from app.application.agent.prompts import FORM_RECORD_GUIDE


class GetWorkoutRecordFormArgs(HarnessToolArgs):
    """训练记录表单：模型可见字段为空，业务日与目录由运行上下文注入。"""


class WorkoutFormPayload(StrictModel):
    """训练记录表单视图：字段清单与提交引导由前端渲染，字段校验归既有记录接口。"""

    form: Literal["workout_record"]
    fields: tuple[str, ...]
    guide: str


def workout_form_payload() -> WorkoutFormPayload:
    """表单载荷：表单标识、字段清单与既有引导文案。"""
    return WorkoutFormPayload(
        form="workout_record",
        fields=WORKOUT_FORM_FIELDS,
        guide=FORM_RECORD_GUIDE,
    )


@tool(args_schema=GetWorkoutRecordFormArgs)
async def get_workout_record_form(
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """读取训练记录表单的字段清单与提交引导；本 Tool 不创建 draft、不写训练记录。"""
    return dump_tool_payload(workout_form_payload())


def workout_form_action() -> dict[str, Any]:
    """``workout_form`` 动作：表单字段清单随动作下行，前端据此渲染打卡表单。"""
    return {"type": "workout_form", **workout_form_payload().model_dump(mode="json")}
