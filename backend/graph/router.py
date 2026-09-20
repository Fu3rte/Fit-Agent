from collections.abc import Mapping, Sequence
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.conversations.context import ContextMessage, history_payload
from graph.model import ModelGateway
from graph.nodes import ModelRequestBudget, request_structured_model
from graph.state import Intent

Domain = Literal[
    "workout_execution",
    "plan_management",
    "analytics",
    "knowledge_qa",
    "general",
]

Action = Literal["query", "create", "modify", "chat"]

ExecutionType = Literal[
    "schedule_query",
    "form_record",
    "natural_language_record",
]

KnowledgeType = Literal["exercise_technique", "methodology"]

ScheduleDay = Literal["today", "tomorrow"]


class RouteKey(NamedTuple):
    """合法路由组合的键：只含判别字段，不含用户填写的动作名与查询日。"""

    domain: Domain
    action: Action
    execution_type: ExecutionType | None
    knowledge_type: KnowledgeType | None


#: 合法组合 → 工作流分支的唯一真相：Schema 校验与下游映射都读这张表。
WORKFLOW_INTENTS: Mapping[RouteKey, Intent] = {
    RouteKey("workout_execution", "query", "schedule_query", None): "view_schedule",
    RouteKey("workout_execution", "create", "form_record", None): "form_record",
    RouteKey(
        "workout_execution", "create", "natural_language_record", None
    ): "natural_language_record",
    RouteKey("plan_management", "create", None, None): "generate_plan",
    RouteKey("plan_management", "modify", None, None): "adjust_plan",
    RouteKey("analytics", "query", None, None): "view_progress",
    RouteKey("knowledge_qa", "query", None, "exercise_technique"): "knowledge_qa",
    RouteKey("knowledge_qa", "query", None, "methodology"): "knowledge_qa",
    RouteKey("general", "chat", None, None): "general",
}


class FitnessIntent(BaseModel):
    """路由结果：请求领域、动作与分支所需的判别参数。"""

    model_config = ConfigDict(extra="forbid")

    domain: Domain = Field(
        description="请求所属领域，取值 workout_execution／plan_management／analytics／knowledge_qa／general 之一。"
    )
    action: Action = Field(
        description="请求动作，取值 query／create／modify／chat 之一，必须与 domain 组成合法组合。"
    )
    execution_type: ExecutionType | None = Field(
        default=None,
        description="workout_execution 分支的执行形态，取值 schedule_query／form_record／natural_language_record；domain 不是 workout_execution 时必须为 null。",
    )
    knowledge_type: KnowledgeType | None = Field(
        default=None,
        description="knowledge_qa 分支的知识类别，取值 exercise_technique／methodology；domain 不是 knowledge_qa 时必须为 null。",
    )
    exercise_name: str | None = Field(
        default=None,
        description="动作名，仅当 domain=knowledge_qa 且 knowledge_type=exercise_technique 时填写；其余任何组合都必须为 null。",
    )
    schedule_day: ScheduleDay | None = Field(
        default=None,
        description="查询日，仅当 execution_type=schedule_query 时取值 today／tomorrow；其余任何组合都必须为 null。",
    )

    @field_validator("exercise_name")
    @classmethod
    def _strip_exercise_name(cls, value: str | None) -> str | None:
        """动作名去掉首尾空白；空白串视为未给出。"""
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def _require_legal_combination(self) -> "FitnessIntent":
        """组合表外的组合与错位参数都在解析期明确失败。"""
        key = route_key(self)
        if key not in WORKFLOW_INTENTS:
            raise ValueError(f"非法路由组合：{key}")
        if self.domain != "knowledge_qa" and self.exercise_name is not None:
            raise ValueError(f"exercise_name 只属于 knowledge_qa：{self.exercise_name!r}")
        if self.knowledge_type == "exercise_technique" and self.exercise_name is None:
            raise ValueError("knowledge_type=exercise_technique 必须给出 exercise_name")
        if self.knowledge_type == "methodology" and self.exercise_name is not None:
            raise ValueError("knowledge_type=methodology 不得给出 exercise_name")
        if self.execution_type == "schedule_query" and self.schedule_day is None:
            raise ValueError("execution_type=schedule_query 必须给出 schedule_day")
        if self.schedule_day is not None and self.execution_type != "schedule_query":
            raise ValueError(f"schedule_day 只属于 schedule_query：{self.schedule_day!r}")
        return self


def route_key(fitness: FitnessIntent) -> RouteKey:
    """路由结果的组合键：判别字段之外的字段不参与分支选择。"""
    return RouteKey(
        fitness.domain, fitness.action, fitness.execution_type, fitness.knowledge_type
    )


def workflow_intent(fitness: FitnessIntent) -> Intent:
    """FitnessIntent → 工作流分支的唯一纯映射；Schema 已拒绝表外组合。"""
    return WORKFLOW_INTENTS[route_key(fitness)]


ROUTER_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的请求路由器，只做一次分类：不执行任何业务动作、不写库、不生成或修改计划、"
    "不做安全判定。只在下列合法组合里选一个，输出结构由 Schema 约束：\n"
    "- workout_execution：本次训练的执行与当前日程。\n"
    "  * action=query、execution_type=schedule_query：查询 active 计划中今天或明天练什么。"
    "用户明确说“明天”时 schedule_day 取 tomorrow；明确说“今天”或未指明日期时取 today。\n"
    "  * action=create、execution_type=form_record：用户要用打卡表单记录训练。\n"
    "  * action=create、execution_type=natural_language_record：用户用自然语言报告一次训练事实，"
    "同时出现记录动词与事实标记。\n"
    "- plan_management：计划版本的管理。action=create 是生成一份新的训练计划；"
    "action=modify 是调整、修改已有训练安排。\n"
    "- analytics：action=query 是查看训练进展、个人最佳或趋势。\n"
    "- knowledge_qa：action=query 是训练知识问答。具体动作规范、发力机制与轨迹归为 "
    "knowledge_type=exercise_technique，exercise_name 必填；减载、渐进式超负荷、疲劳管理与分化思路归为 "
    "knowledge_type=methodology，exercise_name 必须为空。\n"
    "- general：action=chat 是闲聊与不属于上述业务的请求，包含删除数据、查询计划版本、复盘、"
    "伤病判断等超出能力范围的请求。\n"
    "组合表之外的组合一律非法：execution_type 只出现在 workout_execution，knowledge_type 与 "
    "exercise_name 只出现在 knowledge_qa，schedule_day 只出现在 schedule_query。\n"
    "strict Schema 要求输出全部字段：未用到的字段必须在输出里显式给出 null，不得省略。"
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
