from app.agent.tool import AgentTool, ToolDeclaration
from app.agent.tools.common import MainLoopCall
from app.agent.tools.exercises import bind_exercise_tools, exercise_tool_declarations
from app.agent.tools.profile import bind_profile_tools, profile_tool_declarations
from app.application.business.service import BusinessService
from app.domain.business.models import BusinessContext


def business_tool_declarations() -> list[ToolDeclaration]:
    profile = profile_tool_declarations()
    return [profile[0], *exercise_tool_declarations(), *profile[1:]]


def bind_business_tools(
    service: BusinessService,
    context: BusinessContext,
    call: MainLoopCall,
    prepared: dict[str, str],
) -> dict[str, AgentTool]:
    profile = bind_profile_tools(service, context, call, prepared)
    return {
        "get_profile": profile["get_profile"],
        **bind_exercise_tools(service),
        **profile,
    }
