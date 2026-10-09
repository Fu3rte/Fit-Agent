from threading import Event

from app.agent.tool import BeforeToolCall, BeforeToolCallContext, BeforeToolCallResult
from app.agent.tools.common import MainLoopCall
from app.agent.tools.files import check_file_permission
from app.agent.tools.plans import check_plan_permission
from app.agent.tools.profile import check_profile_permission
from app.agent.tools.workouts import check_workout_permission
from app.application.business.service import BusinessService


def create_before_tool_call(service: BusinessService, call: MainLoopCall) -> BeforeToolCall:
    business_rules = {
        "save_profile_update": check_profile_permission,
        "save_plan": check_plan_permission,
        "save_workout": check_workout_permission,
        "update_workout": check_workout_permission,
    }

    async def before_tool_call(
        context: BeforeToolCallContext, signal: Event | None,
    ) -> BeforeToolCallResult | None:
        if signal is not None and signal.is_set():
            return None
        name = context.tool_call.name
        if name in {"write", "edit"}:
            return check_file_permission(context)
        rule = business_rules.get(name)
        if rule is not None:
            return await rule(context, service, call)
        return None

    return before_tool_call
