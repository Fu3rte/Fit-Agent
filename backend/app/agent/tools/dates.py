from datetime import date, timedelta

from app.agent.tool import AgentTool, ExecuteFunction, ToolDeclaration
from app.agent.tools.common import FrozenBusinessContext, envelope, unbound
from app.domain.business.models import BusinessContext, BusinessModel

CALCULATE_DATE_DESCRIPTION = (
    "按后端绑定的可信 business_date 计算相对日期，无副作用。days_offset 为必填严格整数："
    "0 今天、-1 昨天、-N 为 N 天前，正数按同一规则得到未来日期；近七个自然日的起始偏移为 -6，"
    '截止日为可信 business_date。结果为一个 JSON 文本：{"date":"YYYY-MM-DD"}。'
    "禁止传入基准日期、时区或身份字段；计算失败直接反馈。"
)


# 继承业务模型配置：严格整数同时拒绝布尔与浮点写法，额外字段在校验阶段拒绝。
class CalculateDateArguments(BusinessModel):
    days_offset: int


def _tools(
    execute: ExecuteFunction, context: BusinessContext | None = None
) -> dict[str, AgentTool]:
    return {
        "calculate_date": AgentTool(
            "calculate_date",
            CALCULATE_DATE_DESCRIPTION,
            CalculateDateArguments,
            execute,
            max_output_chars=None,
            trusted_context=context,
        ),
    }


def date_tool_declarations() -> list[ToolDeclaration]:
    return [tool.definition() for tool in _tools(unbound).values()]


def bind_date_tools(context: BusinessContext) -> dict[str, AgentTool]:
    # 基准日期取当前批次绑定的可信上下文快照，同批全部调用共用同一 business_date。
    frozen_context = FrozenBusinessContext.model_validate(context.model_dump())

    def calculate_date(
        tool_call_id, params: CalculateDateArguments, signal, on_update
    ):
        offset = date.fromisoformat(frozen_context.business_date) + timedelta(
            days=params.days_offset
        )
        return envelope({"date": offset.isoformat()})

    return _tools(calculate_date, frozen_context)
