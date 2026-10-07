from pydantic import model_validator

from app.agent.tool import AgentTool, ExecuteFunction, ToolDeclaration
from app.agent.tools.common import envelope, unbound
from app.application.business.service import BusinessService
from app.domain.business.models import BusinessModel

SEARCH_EXERCISES_DESCRIPTION = (
    "按中文动作名称包含匹配检索只读动作目录，可用 equipment、body_part、"
    "target、muscle_group 精确筛选；返回匹配动作数组，每项包含九个目录字段。"
)


class SearchExercisesArguments(BusinessModel):
    name: str
    equipment: str | None = None
    body_part: str | None = None
    target: str | None = None
    muscle_group: str | None = None

    @model_validator(mode="after")
    def validate_name(self) -> "SearchExercisesArguments":
        if not self.name.strip():
            raise ValueError("name 必须包含非空白内容")
        return self


def _tool(execute: ExecuteFunction) -> AgentTool:
    return AgentTool(
        "search_exercises",
        SEARCH_EXERCISES_DESCRIPTION,
        SearchExercisesArguments,
        execute,
        max_output_chars=None,
    )


def exercise_tool_declarations() -> list[ToolDeclaration]:
    return [_tool(unbound).definition()]


def bind_exercise_tools(service: BusinessService) -> dict[str, AgentTool]:
    def search_exercises(
        tool_call_id, params: SearchExercisesArguments, signal, on_update
    ):
        results = service.search_exercises(
            params.name,
            equipment=params.equipment,
            body_part=params.body_part,
            target=params.target,
            muscle_group=params.muscle_group,
        )
        return envelope([exercise.model_dump() for exercise in results])

    tool = _tool(search_exercises)
    return {tool.name: tool}
