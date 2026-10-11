from app.agent.tool import AgentTool, ExecuteFunction, ToolDeclaration
from app.agent.tools.common import (
    FrozenBusinessContext,
    MainLoopCall,
    business_result,
    unbound,
)
from app.application.business.service import BusinessService
from app.domain.business.models import (
    BusinessContext,
    PendingProposalArguments,
)

GET_PENDING_PROPOSAL_DESCRIPTION = (
    "按 business_kind 与标准 UUID 的 proposal_id，从对应快照读取仍待确认的完整内容。"
    "business_kind 取 profile、plan 或 workout。返回 business_kind、proposal_id、status、"
    "完整 payload、request_entry_id、source_entry_id、display_entry_id、confirmation_entry_id，"
    "以及该业务已有的准备依据字段：画像为 profile_id、base_profile_version；"
    "计划为 preparation_kind、base_profile_version、base_plan_id；"
    "训练记录为 performed_on、base_workout_id、base_workout_version。"
    "仅在归属当前会话、状态为 pending、请求/来源/展示节点处于当前有效路径、"
    "展示已绑定且准备来源与完整内容一致时返回。其他状态按既有业务错误拒绝，"
    "已保存返回 proposal_already_saved。只读，不创建、修改或保存任何数据。"
)


def _tools(get_pending_proposal: ExecuteFunction) -> dict[str, AgentTool]:
    return {
        "get_pending_proposal": AgentTool(
            "get_pending_proposal",
            GET_PENDING_PROPOSAL_DESCRIPTION,
            PendingProposalArguments,
            get_pending_proposal,
            max_output_chars=None,
        ),
    }


def pending_proposal_tool_declarations() -> list[ToolDeclaration]:
    return [tool.definition() for tool in _tools(unbound).values()]


def bind_pending_proposal_tools(
    service: BusinessService,
    context: BusinessContext,
    call: MainLoopCall,
) -> dict[str, AgentTool]:
    frozen_context = FrozenBusinessContext.model_validate(context.model_dump())

    def get_pending_proposal(
        tool_call_id, params: PendingProposalArguments, signal, on_update
    ):
        return business_result(
            service.get_pending_proposal(frozen_context, params, signal), call
        )

    return _tools(get_pending_proposal)
