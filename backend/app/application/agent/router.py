from collections.abc import Mapping, Sequence
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.agent.budget import ModelRequestBudget, request_structured_model
from app.application.agent.contracts import Intent
from app.application.ports import ModelGateway
from app.domain.conversations.context import ContextMessage, history_payload

Domain = Literal[
    "workout_execution",
    "plan_management",
    "analytics",
    "general",
]

Action = Literal["query", "create", "modify", "chat"]

ExecutionType = Literal[
    "schedule_query",
    "form_record",
    "natural_language_record",
]


class RouteKey(NamedTuple):
    """合法路由组合的键：只含判别字段，不含用户填写的查询日。"""

    domain: Domain
    action: Action
    execution_type: ExecutionType | None


#: 合法组合 → 工作流分支的唯一真相：Schema 校验与下游映射都读这张表。
WORKFLOW_INTENTS: Mapping[RouteKey, Intent] = {
    RouteKey("workout_execution", "query", "schedule_query"): "view_schedule",
    RouteKey("workout_execution", "create", "form_record"): "form_record",
    RouteKey(
        "workout_execution", "create", "natural_language_record"
    ): "natural_language_record",
    RouteKey("plan_management", "create", None): "generate_plan",
    RouteKey("plan_management", "modify", None): "adjust_plan",
    RouteKey("analytics", "query", None): "view_progress",
    RouteKey("general", "chat", None): "general",
}


class FitnessIntent(BaseModel):
    """路由结果：请求领域、动作与分支所需的判别参数。"""

    model_config = ConfigDict(extra="forbid")

    domain: Domain = Field(
        description="请求所属领域，取值 workout_execution／plan_management／analytics／general 之一。"
    )
    action: Action = Field(
        description="请求动作，取值 query／create／modify／chat 之一，必须与 domain 组成合法组合。"
    )
    execution_type: ExecutionType | None = Field(
        default=None,
        description="workout_execution 分支的执行形态，取值 schedule_query／form_record／natural_language_record；domain 不是 workout_execution 时必须为 null。",
    )

    @model_validator(mode="after")
    def _require_legal_combination(self) -> "FitnessIntent":
        """组合表外的组合在解析期明确失败。"""
        key = route_key(self)
        if key not in WORKFLOW_INTENTS:
            raise ValueError(f"非法路由组合：{key}")
        return self


def route_key(fitness: FitnessIntent) -> RouteKey:
    """路由结果的组合键：判别字段之外的字段不参与分支选择。"""
    return RouteKey(fitness.domain, fitness.action, fitness.execution_type)


def workflow_intent(fitness: FitnessIntent) -> Intent:
    """FitnessIntent → 工作流分支的唯一纯映射；Schema 已拒绝表外组合。"""
    return WORKFLOW_INTENTS[route_key(fitness)]


NON_PLAN_INTENTS: tuple[Intent, ...] = (
    "form_record",
    "natural_language_record",
    "view_progress",
    "view_schedule",
    "general",
)

#: 走只读工具 harness 的两个 intent：最终文本仍回既有 ``message``／``done``。
TOOL_INTENTS: tuple[Intent, ...] = ("view_schedule", "view_progress")


ROUTER_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的请求路由器，只做一次分类：不执行任何业务动作、不写库、不生成或修改计划、"
    "不做安全判定。只在下列合法组合里选一个，输出结构由 Schema 约束：\n"
    "- workout_execution：本次训练的执行与当前日程。\n"
    "  * action=query、execution_type=schedule_query：查询当前 active 计划与训练日历，包含今天、"
    "明天、后天、本周五、下周一、指定 ISO 日期以及这个月、下个月等全部日程问法；不提取日期参数。\n"
    "  * action=create、execution_type=form_record：用户要用打卡表单记录训练。\n"
    "  * action=create、execution_type=natural_language_record：用户用自然语言报告一次训练事实，"
    "同时出现记录动词与事实标记。\n"
    "- plan_management：计划版本的管理。action=create 是生成一份新的训练计划；"
    "action=modify 是调整、修改已有训练安排。\n"
    "- analytics：action=query 是查看训练进展，覆盖个人最佳、趋势、最近训练与历史训练内容。\n"
    "- general：action=chat 是闲聊、训练知识问答与不属于上述业务的请求。训练知识问答包含具体动作"
    "规范、发力机制与轨迹，以及减载、渐进式超负荷、疲劳管理与分化思路等问法；还包含删除数据、"
    "查询计划版本、复盘、伤病判断等超出能力范围的请求。\n"
    "组合表之外的组合一律非法：execution_type 只出现在 workout_execution。\n"
    "strict Schema 只含 domain、action、execution_type 三个字段：未用到的 execution_type 必须在"
    "输出里显式给出 null，不得省略。"
    "每个字段的语义以 Schema 的字段描述为准，描述与本节规则一致。"
)


async def classify_intent(
    request: str,
    *,
    model: ModelGateway,
    budget: ModelRequestBudget,
    history: Sequence[ContextMessage] = (),
) -> FitnessIntent:
    """路由的唯一入口：每个请求都调一次结构化分类，不设短语旁路。

    ``history`` 是本次请求之前的对话上下文投影；为空时 payload 只有 ``request``，
    当前用户请求因此只出现一次。
    """
    payload: dict[str, object] = {
        "request": request.strip(),
        **history_payload(history),
    }
    return await request_structured_model(
        model,
        ROUTER_SYSTEM_PROMPT,
        payload,
        budget,
        FitnessIntent,
    )
