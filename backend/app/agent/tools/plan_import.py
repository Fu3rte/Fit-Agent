from app.agent.tool import AgentTool, ExecuteFunction, ToolDeclaration
from app.agent.tools.common import (
    FrozenBusinessContext,
    MainLoopCall,
    business_result,
    unbound,
)
from app.ai.messages import JsonObject
from app.application.business.service import BusinessService
from app.domain.business.models import (
    BusinessContext,
    PlanAdjustmentArguments,
    PlanImportArguments,
)


def _prepare_plan_import_arguments(args: JsonObject) -> JsonObject:
    # 仅将两个依据字段的精确字符串 "None" 归一为 null，其他字段与值原样交给严格校验。
    for field in ("base_profile_version", "base_plan_id"):
        if args.get(field) == "None":
            args[field] = None
    return args


def _tools(
    prepare_import: ExecuteFunction,
    prepare_adjustment: ExecuteFunction,
    context: BusinessContext | None = None,
) -> dict[str, AgentTool]:
    basis = (
        "全部字段必填，未知值保持JSON null，未知动作详情保持空数组。"
        "base_profile_version取实时画像版本，无画像传null；base_plan_id取实时当前计划id，"
        "无当前计划传null，上传计划调整同样使用该依据。准备类型由后端决定。"
        '两依据字段未知时写JSON null字面量，禁止字符串"null"、空字符串、数字0或省略；'
        '参数预处理仅将两依据字段精确字符串"None"转为null，其他字段和值保持原样。'
        "notes完整记录依据和理由，suggested_fields标记助手建议来源。"
        "仅创建待确认快照，完整结果持久化展示后等待后续用户自然语言明确确认，"
        "再共用save_plan保存；本调用不授权或保存计划。"
    )
    return {
        "prepare_plan_import": AgentTool(
            "prepare_plan_import",
            "录入用户提供的现有计划，保留已知内容及未知字段，无建档前置要求。" + basis,
            PlanImportArguments,
            prepare_import,
            execution_mode="sequential",
            prepare_arguments=_prepare_plan_import_arguments,
            max_output_chars=None,
            trusted_context=context,
        ),
        "prepare_plan_adjustment": AgentTool(
            "prepare_plan_adjustment",
            "按用户本次任务准备完整调整结果，支持直接调整上传计划；"
            "仅索取完成本次任务必要的信息，其余未知字段保留。" + basis,
            PlanAdjustmentArguments,
            prepare_adjustment,
            execution_mode="sequential",
            prepare_arguments=_prepare_plan_import_arguments,
            max_output_chars=None,
            trusted_context=context,
        ),
    }


def plan_import_tool_declarations() -> list[ToolDeclaration]:
    return [tool.definition() for tool in _tools(unbound, unbound).values()]


def bind_plan_import_tools(
    service: BusinessService,
    context: BusinessContext,
    call: MainLoopCall,
    prepared: dict[str, str],
) -> dict[str, AgentTool]:
    frozen_context = FrozenBusinessContext.model_validate(context.model_dump())

    def prepare_import(tool_call_id, params: PlanImportArguments, signal, on_update):
        def register(proposal):
            prepared[tool_call_id] = proposal.proposal_id

        return business_result(
            service.prepare_plan_import(frozen_context, params, signal), call, register
        )

    def prepare_adjustment(tool_call_id, params: PlanAdjustmentArguments, signal, on_update):
        def register(proposal):
            prepared[tool_call_id] = proposal.proposal_id

        return business_result(
            service.prepare_plan_adjustment(frozen_context, params, signal), call, register
        )

    return _tools(prepare_import, prepare_adjustment, frozen_context)
