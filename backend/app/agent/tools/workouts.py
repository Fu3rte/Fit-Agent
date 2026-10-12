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
    WorkoutGetArguments,
    WorkoutListArguments,
    WorkoutProposalArguments,
    WorkoutStatusArguments,
)


def _prepare_workout_arguments(args: JsonObject) -> JsonObject:
    for field in ("base_workout_id", "base_workout_version"):
        if args.get(field) == "None":
            args[field] = None
    return args


def _tools(
    get_workout: ExecuteFunction,
    list_workouts: ExecuteFunction,
    prepare_workout: ExecuteFunction,
    get_workout_save_status: ExecuteFunction,
) -> dict[str, AgentTool]:
    return {
        "get_workout": AgentTool(
            "get_workout", "按标准 UUID 读取完整最新实际训练记录。",
            WorkoutGetArguments, get_workout, max_output_chars=None,
        ),
        "list_workouts": AgentTool(
            "list_workouts",
            "查询完整实际训练记录，日期范围含边界，日期倒序；默认第1页、每页10条。"
            "查询同一天时 date_from 和 date_to 传相同具体日期，先读取再整理当天内容。",
            WorkoutListArguments, list_workouts, max_output_chars=None,
        ),
        "prepare_workout": AgentTool(
            "prepare_workout",
            "创建完整实际训练待确认快照，不保存记录。必填 performed_on、base_workout_id、"
            "base_workout_version、payload；基础ID/版本使用先前查询的原值，新增两键必须存在且值为JSON null字面量，"
            '参数预处理仅将两基础字段精确字符串"None"转为null，其他字段和值保持原样；'
            '禁止"null"、0或省略。示例：{"base_workout_id":null,"base_workout_version":null}。'
            "同日已有记录时重写完整当天内容，保留本次未涉及的内容。结果持久化后绑定真实展示节点，"
            "完整payload由结果展示，等待用户点击该快照的确认按钮。日期禁止晚于可信business_date。",
            WorkoutProposalArguments, prepare_workout,
            execution_mode="sequential", prepare_arguments=_prepare_workout_arguments,
            max_output_chars=None,
        ),
        "get_workout_save_status": AgentTool(
            "get_workout_save_status",
            "按原proposal_id核对训练新增或更新操作。返回proposal_id/status/result，状态为"
            "pending/processing/saved/invalidated/conflicted，仅saved携带完整固定结果。"
            "固定结果可为较早版本；记录查询返回最新版本。结果核实前禁止创建新的保存操作。",
            WorkoutStatusArguments, get_workout_save_status, max_output_chars=None,
        ),
    }


def workout_tool_declarations() -> list[ToolDeclaration]:
    return [
        tool.definition()
        for tool in _tools(unbound, unbound, unbound, unbound).values()
    ]


def bind_workout_tools(
    service: BusinessService,
    context: BusinessContext,
    call: MainLoopCall,
    prepared: dict[str, str],
) -> dict[str, AgentTool]:
    context = FrozenBusinessContext.model_validate(context.model_dump())

    def get_workout(tool_call_id, params: WorkoutGetArguments, signal, on_update):
        return business_result(service.get_workout(params.workout_id), call)

    def list_workouts(tool_call_id, params: WorkoutListArguments, signal, on_update):
        return business_result(service.list_workouts(params), call)

    def prepare_workout(tool_call_id, params: WorkoutProposalArguments, signal, on_update):
        def register(proposal):
            prepared[tool_call_id] = proposal.proposal_id

        return business_result(service.prepare_workout(context, params, signal), call, register)

    def get_workout_save_status(tool_call_id, params: WorkoutStatusArguments, signal, on_update):
        return business_result(
            service.get_workout_save_status(context, params.proposal_id, signal), call
        )

    return _tools(get_workout, list_workouts, prepare_workout, get_workout_save_status)
