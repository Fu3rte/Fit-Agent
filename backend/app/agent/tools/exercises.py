import json
from functools import partial

from app.agent.tool import AgentTool, AgentToolResult, ExecuteFunction, ToolDeclaration
from app.agent.tools.common import MainLoopCall, envelope, unbound
from app.application.business.retrieval_response import DETAILS_BUDGET, SEARCH_BUDGET
from app.application.business.service import BusinessService
from app.domain.business.errors import BusinessError
from app.domain.business.models import (
    CatalogSearchArguments,
    ExerciseDetailsArguments,
)

SEARCH_EXERCISES_DESCRIPTION = (
    "按语义查询检索只读动作目录，支持中文、英文、别名与描述文本；purpose 区分用途："
    "lookup 用于核实具体动作事实（允许返回画像受限动作），generation 用于生成计划建议，"
    "replacement 用于替换动作（generation/replacement 应用环境默认、可用/不可用器械与排除动作）。"
    "name 为语义查询；equipment、body_part、target、muscle_group 为目录 canonical 精确筛选。"
    "task_environment、available_equipment、unavailable_equipment、excluded_ids 为本次任务条件，"
    "不写入长期画像；available_equipment 表示本次明确可用声明（并入环境默认集合），"
    "\"只用某种器械\"应通过 equipment 精确筛选表达。返回 items、has_more、remaining_ids："
    "items 每项含 id、name、equipment、target 与可核实的 matches 说明；"
    "如需完整步骤与重量口径，按返回的 id 调用 get_exercise_details。"
)

GET_EXERCISE_DETAILS_DESCRIPTION = (
    "按动作 ID 批量读取只读动作目录的完整事实，最多 5 个；返回 items、has_more、remaining_ids，"
    "每项包含九个目录字段及完整中英文步骤与重量口径。输入 ids 按原顺序返回，"
    "未知、空白、重复或超过上限明确报错。详情读取只核实事实，不构成推荐或保存授权。"
)


FRAMEWORK_ERRORS = {
    "tool_not_authorized": "工具未授权或已被移除。",
    "argument_preparation_failed": "参数预处理失败。",
    "permission_check_failed": "权限检查失败。",
    "tool_blocked": "工具执行被拒绝。",
    "tool_execution_failed": "工具执行失败。",
    "tool_postprocess_failed": "工具后处理失败；请核对原操作。",
}


def _reject_json_constant(value: str) -> None:
    raise ValueError("检索工具响应包含非法 JSON 常量")


def _validate_retrieval_output(result: AgentToolResult, *, budget: int) -> None:
    text = "".join(block.text for block in result.content)
    if len(text) > budget:
        raise ValueError("检索工具响应超出完整 JSON 预算")
    payload = json.loads(text, parse_constant=_reject_json_constant)
    if not isinstance(payload, dict):
        raise ValueError("检索工具响应必须为 JSON 对象")


def _retrieval_envelope(
    payload: object, is_error: bool = False, *, budget: int
) -> AgentToolResult:
    result = envelope(payload, is_error)
    _validate_retrieval_output(result, budget=budget)
    return result


def _framework_error(code: str, *, budget: int) -> AgentToolResult:
    if code.startswith("invalid_arguments."):
        field = code.removeprefix("invalid_arguments.")
        payload = {
            "code": "invalid_tool_arguments",
            "message": "参数校验失败。",
            "parameter": field,
        }
    else:
        payload = {"code": code, "message": FRAMEWORK_ERRORS[code]}
    return _retrieval_envelope(payload, True, budget=budget)


def _search_tool(execute: ExecuteFunction) -> AgentTool:
    return AgentTool(
        "search_exercises",
        SEARCH_EXERCISES_DESCRIPTION,
        CatalogSearchArguments,
        execute,
        max_output_chars=None,
        format_error=partial(_framework_error, budget=SEARCH_BUDGET),
        validate_output=partial(_validate_retrieval_output, budget=SEARCH_BUDGET),
    )


def _details_tool(execute: ExecuteFunction) -> AgentTool:
    return AgentTool(
        "get_exercise_details",
        GET_EXERCISE_DETAILS_DESCRIPTION,
        ExerciseDetailsArguments,
        execute,
        max_output_chars=None,
        format_error=partial(_framework_error, budget=DETAILS_BUDGET),
        validate_output=partial(_validate_retrieval_output, budget=DETAILS_BUDGET),
    )


def exercise_tool_declarations() -> list[ToolDeclaration]:
    return [
        _search_tool(unbound).definition(),
        _details_tool(unbound).definition(),
    ]


def bind_exercise_tools(
    service: BusinessService, call: MainLoopCall
) -> dict[str, AgentTool]:
    def search_exercises(
        tool_call_id, params: CatalogSearchArguments, signal, on_update
    ):
        # 搜索资源属于主事件循环：经 MainLoopCall 投递回主循环执行，不另起 SDK 循环。
        # 工具取消信号随调用桥接到主循环检索任务，阶段间检查并在真实 RPC 收尾后停止后续编码。
        try:
            result = call(service.search_exercises(params, signal=signal))
        except BusinessError as error:
            return _retrieval_envelope(error.detail(), True, budget=SEARCH_BUDGET)
        return _retrieval_envelope(result.model_dump(), budget=SEARCH_BUDGET)

    def get_exercise_details(
        tool_call_id, params: ExerciseDetailsArguments, signal, on_update
    ):
        try:
            result = service.exercise_details(params)
        except BusinessError as error:
            return _retrieval_envelope(error.detail(), True, budget=DETAILS_BUDGET)
        return _retrieval_envelope(result.model_dump(), budget=DETAILS_BUDGET)

    return {
        "search_exercises": _search_tool(search_exercises),
        "get_exercise_details": _details_tool(get_exercise_details),
    }
