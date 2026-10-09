from dataclasses import replace

from app.agent.tool import (
    AgentTool,
    ExecuteFunction,
    ToolDeclaration,
)
from app.agent.tools.common import (
    FrozenBusinessContext,
    MainLoopCall,
    business_result,
    check_business_permission,
    unbound,
)
from app.ai.messages import JsonObject
from app.application.business.service import BusinessService
from app.domain.business.models import (
    BusinessContext,
    BusinessModel,
    ProfileProposalArguments,
    ProfileSaveArguments,
    ProfileStatusArguments,
)

GET_PROFILE_DESCRIPTION = (
    "读取已保存的个人画像，返回 version 与 content；未建档时两者均为 null。"
)
PREPARE_PROFILE_UPDATE_DESCRIPTION = (
    "整理完整画像并创建待确认快照；传入 profile_id=1、base_profile_version 与完整 payload，"
    "返回 proposal_id、profile_id、base_profile_version 及 payload。"
    "base_profile_version 必须是 get_profile 返回的 version 原值：已建档时为正整数，"
    '未建档时该键必须存在且值为 JSON null；参数预处理仅将该字段的精确字符串 "None" 转为 null，'
    '其他字段和值保持原样，禁止使用 "null"、0 或省略。'
    "结果节点持久化后由后端绑定为展示消息，完整画像由该结果展示，无需重复输出；"
    "本工具不保存画像，须等待用户后续自然语言确认。"
)
SAVE_PROFILE_UPDATE_DESCRIPTION = (
    "保存用户已明确确认的画像快照；输入 proposal_id、该快照展示消息节点的 "
    "display_entry_id、用户确认消息节点的 confirmation_entry_id，节点 ID 取上下文"
    "message_nodes 中的真实值。成功返回 proposal_id、profile_id、version、content、"
    "saved_at；重复调用返回原结果。"
)
GET_PROFILE_UPDATE_STATUS_DESCRIPTION = (
    "查询画像快照的保存状态；输入 proposal_id，返回 proposal_id、status、result。"
    "status 为 pending / processing / saved / invalidated / conflicted，仅 saved 时 "
    "result 为完整保存结果，其他状态为 null。保存结果未知时以此核对原操作。"
)


class GetProfileArguments(BusinessModel):
    pass


def _prepare_profile_update_arguments(args: JsonObject) -> JsonObject:
    if args.get("base_profile_version") == "None":
        args["base_profile_version"] = None
    return args


def _tools(
    get_profile: ExecuteFunction,
    prepare_profile_update: ExecuteFunction,
    save_profile_update: ExecuteFunction,
    get_profile_update_status: ExecuteFunction,
) -> dict[str, AgentTool]:
    # 画像工具返回完整 JSON，关闭输出截断。
    return {
        "get_profile": AgentTool(
            "get_profile",
            GET_PROFILE_DESCRIPTION,
            GetProfileArguments,
            get_profile,
            max_output_chars=None,
        ),
        "prepare_profile_update": AgentTool(
            "prepare_profile_update",
            PREPARE_PROFILE_UPDATE_DESCRIPTION,
            ProfileProposalArguments,
            prepare_profile_update,
            execution_mode="sequential",
            prepare_arguments=_prepare_profile_update_arguments,
            max_output_chars=None,
        ),
        "save_profile_update": AgentTool(
            "save_profile_update",
            SAVE_PROFILE_UPDATE_DESCRIPTION,
            ProfileSaveArguments,
            save_profile_update,
            execution_mode="sequential",
            max_output_chars=None,
        ),
        "get_profile_update_status": AgentTool(
            "get_profile_update_status",
            GET_PROFILE_UPDATE_STATUS_DESCRIPTION,
            ProfileStatusArguments,
            get_profile_update_status,
            max_output_chars=None,
        ),
    }


def profile_tool_declarations() -> list[ToolDeclaration]:
    return [
        tool.definition()
        for tool in _tools(unbound, unbound, unbound, unbound).values()
    ]


def bind_profile_tools(
    service: BusinessService,
    context: BusinessContext,
    call: MainLoopCall,
    prepared: dict[str, str],
) -> dict[str, AgentTool]:
    # call 将业务协程投递到数据库所在主循环；prepared 记录本批快照展示绑定。
    context = FrozenBusinessContext.model_validate(context.model_dump())

    def get_profile(tool_call_id, params: GetProfileArguments, signal, on_update):
        return business_result(service.get_profile(), call)

    def prepare_profile_update(
        tool_call_id, params: ProfileProposalArguments, signal, on_update
    ):
        def register(proposal):
            prepared[tool_call_id] = proposal.proposal_id

        return business_result(
            service.prepare_profile_update(context, params, signal), call, register
        )

    def save_profile_update(
        tool_call_id, params: ProfileSaveArguments, signal, on_update
    ):
        return business_result(service.save_profile_update(context, params, signal), call)

    def get_profile_update_status(
        tool_call_id, params: ProfileStatusArguments, signal, on_update
    ):
        return business_result(
            service.get_profile_update_status(context, params.proposal_id, signal), call
        )

    tools = _tools(
        get_profile,
        prepare_profile_update,
        save_profile_update,
        get_profile_update_status,
    )
    tools["save_profile_update"] = replace(
        tools["save_profile_update"], trusted_context=context
    )
    return tools


async def check_profile_permission(context, service: BusinessService, call: MainLoopCall):
    return await check_business_permission(
        context, service.check_profile_save_authorization, call
    )
