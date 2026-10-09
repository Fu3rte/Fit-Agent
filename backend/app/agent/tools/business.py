from app.agent.tool import AgentTool, ToolDeclaration
from app.agent.tools.common import MainLoopCall
from app.agent.tools.dates import bind_date_tools, date_tool_declarations
from app.agent.tools.exercises import bind_exercise_tools, exercise_tool_declarations
from app.agent.tools.plan_import import (
    bind_plan_import_tools,
    plan_import_tool_declarations,
)
from app.agent.tools.plans import bind_plan_tools, plan_tool_declarations
from app.agent.tools.profile import bind_profile_tools, profile_tool_declarations
from app.agent.tools.workouts import bind_workout_tools, workout_tool_declarations
from app.application.business.service import BusinessService
from app.domain.business.models import BusinessContext


def business_tool_declarations() -> list[ToolDeclaration]:
    profile = profile_tool_declarations()
    return [profile[0], *exercise_tool_declarations(), *date_tool_declarations(),
            *profile[1:], *workout_tool_declarations(),
            *plan_tool_declarations(), *plan_import_tool_declarations()]


def bind_business_tools(
    service: BusinessService,
    context: BusinessContext,
    call: MainLoopCall,
    prepared: dict[str, str],
    workout_prepared: dict[str, str],
    plan_prepared: dict[str, str] | None = None,
) -> dict[str, AgentTool]:
    profile = bind_profile_tools(service, context, call, prepared)
    plan_registry = {} if plan_prepared is None else plan_prepared
    return {
        "get_profile": profile["get_profile"],
        **bind_exercise_tools(service),
        **bind_date_tools(context),
        **profile,
        **bind_workout_tools(service, context, call, workout_prepared),
        **bind_plan_tools(service, context, call, plan_registry),
        **bind_plan_import_tools(service, context, call, plan_registry),
    }
