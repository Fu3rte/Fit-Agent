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
            "按用户本次任务准备完整最终payload，支持调整当前计划及直接调整上传计划。"
            "准备前get_profile读取真实画像状态与版本，get_current_plan读取真实当前计划ID与完整内容；"
            "明确调整对象、目标、训练日及动作位置，对象或位置歧义时询问，无待调整内容时索取。"
            "固定查询list_workouts(date_from=null, date_to=business_context.business_date, page=1, page_size=10)，"
            "date_from须省略该键或为JSON null，禁止写成字符串\"None\"或\"null\"；"
            "business_date来自后端可信运行上下文，读取返回的全部items；总数为x时使用min(x, 10)条，"
            "0条为空数组[]。每条为一个训练日期的完整最新记录，按performed_on降序、id降序，"
            "包含实际动作、逐组表现、重量口径及训练notes，使用其中与本次任务相关的表现。"
            "按任务判断信息充分性，仅索取必要信息，缺失信息保持未知，歧义及依据不足通过对话澄清。"
            "结构或频率调整允许空动作详情；指定动作改组数须明确位置；替换动作须核实目录事实、"
            "相关伤病及器械限制；按表现调整须明确相关逐组数据、重量口径及必要反馈。"
            "需要补充或更新画像时完成画像展示、后续确认及保存，取得真实新版本。"
            "全部声明字段均须提交，保留未涉及字段、原有说明及suggested_fields建议来源；"
            "未知标量为null，未知动作详情及休息日exercises=[]，days至少一项；"
            "重量有值（包括0）须明确load_convention，目录外动作保留名称和exercise_id=null，"
            "说明核实状态并澄清相关限制，完整结果遵守已知伤病、动作和器械限制。"
            "suggested_fields按最终内容用JSON Pointer标记助手补充或修改的建议，数组位置变化时同步来源路径。"
            "计划或训练日notes写明修改位置、原值、新值、理由、实际训练依据、记录日期、必要重量口径、"
            "不确定性及注意事项，包含删除内容的说明；这些说明必须写入payload的notes，"
            "不能只写在对话回复文本中，原有suggested_fields必须全部保留。"
            "提案固定使用准备时查询到的训练事实，新增或修正训练记录保持原提案内容及状态；"
            "用户要求采用新事实时重新查询、准备完整提案、展示全部业务字段、notes、建议来源及所用依据，"
            "等待后续再次明确确认；修改式确认按修改处理，同会话新快照成功使旧pending失效。"
            "保存使用business_context.message_nodes中business_kind=plan的真实快照、展示及后续确认绑定；"
            "一条确认跨画像、计划及训练记录仅授权一份快照，结果未知时按原proposal_id调用"
            "get_plan_save_status核对原操作，核实前禁止创建新的保存操作。" + basis,
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
