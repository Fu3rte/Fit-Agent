from app.agent.tool import AgentTool, ExecuteFunction, ToolDeclaration
from app.agent.tools.common import (
    FrozenBusinessContext,
    MainLoopCall,
    business_result,
    envelope,
    unbound,
)
from app.ai.messages import JsonObject
from app.application.business.service import BusinessService
from app.domain.business.models import (
    BusinessContext,
    BusinessModel,
    PlanGetArguments,
    PlanProposalArguments,
    PlanStatusArguments,
)


class EmptyPlanArguments(BusinessModel):
    pass


def _prepare_plan_arguments(args: JsonObject) -> JsonObject:
    # 仅将顶层 base_plan_id 的精确字符串 "None" 归一为 null，其他字段与值原样交给严格校验。
    if args.get("base_plan_id") == "None":
        args["base_plan_id"] = None
    return args


def _tools(
    get_current_plan: ExecuteFunction,
    get_plan: ExecuteFunction,
    list_plans: ExecuteFunction,
    prepare_plan: ExecuteFunction,
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
            '参数预处理仅将base_plan_id精确字符串"None"转为JSON null，其他字段和值原样严格校验。'
            "训练日必须包含具体动作，每个动作sets有值，reps或duration_seconds至少一个有值。"
            "生成调用参数校验失败时，依据返回的实际错误修正原prepare_plan调用后重试；"
            "禁止改用prepare_plan_import或prepare_plan_adjustment绕过生成约束。"
            "结果持久化后绑定真实展示节点，完整payload由结果展示，等待用户点击该快照的确认按钮。"
            "用户修改建议时创建新快照并重新展示确认；尚未采纳的生成建议经用户修改时，"
            "继续使用prepare_plan创建新快照。",
            PlanProposalArguments, prepare_plan, execution_mode="sequential",
            prepare_arguments=_prepare_plan_arguments, max_output_chars=None,
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
    return [tool.definition() for tool in _tools(unbound, unbound, unbound, unbound, unbound).values()]


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

    def get_plan_save_status(tool_call_id, params: PlanStatusArguments, signal, on_update):
        return business_result(service.get_plan_save_status(context, params.proposal_id, signal), call)

    return _tools(get_current_plan, get_plan, list_plans, prepare_plan, get_plan_save_status)
