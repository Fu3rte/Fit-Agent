from dataclasses import replace

from app.agent.tool import AgentTool, ExecuteFunction, ToolDeclaration
from app.agent.tools.common import (
    FrozenBusinessContext,
    MainLoopCall,
    business_result,
    check_business_permission,
    envelope,
    unbound,
)
from app.application.business.service import BusinessService
from app.domain.business.models import (
    BusinessContext,
    BusinessModel,
    PlanGetArguments,
    PlanProposalArguments,
    PlanSaveArguments,
    PlanStatusArguments,
)


class EmptyPlanArguments(BusinessModel):
    pass


def _tools(
    get_current_plan: ExecuteFunction,
    get_plan: ExecuteFunction,
    list_plans: ExecuteFunction,
    prepare_plan: ExecuteFunction,
    save_plan: ExecuteFunction,
    get_plan_save_status: ExecuteFunction,
) -> dict[str, AgentTool]:
    return {
        "get_current_plan": AgentTool(
            "get_current_plan", "读取当前计划的id和完整content；没有当前计划时两者为null。",
            EmptyPlanArguments, get_current_plan, max_output_chars=None,
        ),
        "get_plan": AgentTool(
            "get_plan", "按标准UUID读取完整计划版本，包含is_current和created_at。",
            PlanGetArguments, get_plan, max_output_chars=None,
        ),
        "list_plans": AgentTool(
            "list_plans", "返回全部完整计划版本的直接数组，按created_at、id降序排列；没有版本时返回[]。",
            EmptyPlanArguments, list_plans, max_output_chars=None,
        ),
        "prepare_plan": AgentTool(
            "prepare_plan",
            "创建完整训练计划待确认快照。base_profile_version使用已保存画像的版本，"
            "base_plan_id使用当前计划id原值，无当前计划时传JSON null；payload是完整内容。"
            "训练日必须包含具体动作，每个动作sets有值，reps或duration_seconds至少一个有值。"
            "结果持久化后绑定真实展示节点，完整payload由结果展示，等待后续用户自然语言明确确认。"
            "用户修改建议时创建新快照并重新展示确认。",
            PlanProposalArguments, prepare_plan, execution_mode="sequential", max_output_chars=None,
        ),
        "save_plan": AgentTool(
            "save_plan",
            "保存后续用户明确确认的计划快照。输入proposal_id、display_entry_id、confirmation_entry_id，"
            "引用business_kind=plan的真实节点绑定。新增完整版本并设为当前计划，返回完整固定结果。"
            "重复调用返回原id、内容和时间；依据冲突须重新查询、准备、展示并确认。",
            PlanSaveArguments, save_plan, execution_mode="sequential", max_output_chars=None,
        ),
        "get_plan_save_status": AgentTool(
            "get_plan_save_status",
            "按原proposal_id核对计划保存状态，返回proposal_id/status/result。"
            "状态为pending/processing/saved/invalidated/conflicted，仅saved携带完整固定结果。"
            "版本是否当前通过计划查询核对；结果未知时核对原操作。",
            PlanStatusArguments, get_plan_save_status, max_output_chars=None,
        ),
    }


def plan_tool_declarations() -> list[ToolDeclaration]:
    return [tool.definition() for tool in _tools(unbound, unbound, unbound, unbound, unbound, unbound).values()]


def bind_plan_tools(
    service: BusinessService,
    context: BusinessContext,
    call: MainLoopCall,
    prepared: dict[str, str],
) -> dict[str, AgentTool]:
    context = FrozenBusinessContext.model_validate(context.model_dump())

    def get_current_plan(tool_call_id, params: EmptyPlanArguments, signal, on_update):
        return business_result(service.get_current_plan(), call)

    def get_plan(tool_call_id, params: PlanGetArguments, signal, on_update):
        return business_result(service.get_plan(params.plan_id), call)

    def list_plans(tool_call_id, params: EmptyPlanArguments, signal, on_update):
        return envelope([item.model_dump() for item in call(service.list_plans())])

    def prepare_plan(tool_call_id, params: PlanProposalArguments, signal, on_update):
        def register(proposal):
            prepared[tool_call_id] = proposal.proposal_id

        return business_result(service.prepare_plan(context, params, signal), call, register)

    def save_plan(tool_call_id, params: PlanSaveArguments, signal, on_update):
        return business_result(service.save_plan(context, params, signal), call)

    def get_plan_save_status(tool_call_id, params: PlanStatusArguments, signal, on_update):
        return business_result(service.get_plan_save_status(context, params.proposal_id, signal), call)

    tools = _tools(get_current_plan, get_plan, list_plans, prepare_plan, save_plan, get_plan_save_status)
    tools["save_plan"] = replace(tools["save_plan"], trusted_context=context)
    return tools


async def check_plan_permission(context, service: BusinessService, call: MainLoopCall):
    return await check_business_permission(context, service.check_plan_save_authorization, call)
