# 路由契约的纯测试：合法组合表、非法组合拒绝、可穷尽映射与统一结构化路由调用。
# 依据：本轮拍板的 FitnessIntent Schema（domain／action／execution_type／knowledge_type／
# exercise_name，日期槽已移除）、唯一纯映射函数 FitnessIntent → Intent、取消确定性短语旁路。

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import product
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from graph.model import dump_model_payload
from graph.nodes import ModelRequestBudget
from graph.router import (
    ROUTER_SYSTEM_PROMPT,
    WORKFLOW_INTENTS,
    Action,
    Domain,
    ExecutionType,
    FitnessIntent,
    KnowledgeType,
    RouteKey,
    classify_intent,
    workflow_intent,
)
from graph.state import Intent

#: 合法组合表整表冻结：改表、删行或新增行都会失败。
LEGAL_ROUTES: tuple[tuple[RouteKey, Intent], ...] = (
    (RouteKey("workout_execution", "query", "schedule_query", None), "view_schedule"),
    (RouteKey("workout_execution", "create", "form_record", None), "form_record"),
    (
        RouteKey("workout_execution", "create", "natural_language_record", None),
        "natural_language_record",
    ),
    (RouteKey("plan_management", "create", None, None), "generate_plan"),
    (RouteKey("plan_management", "modify", None, None), "adjust_plan"),
    (RouteKey("analytics", "query", None, None), "view_progress"),
    (RouteKey("knowledge_qa", "query", None, "exercise_technique"), "knowledge_qa"),
    (RouteKey("knowledge_qa", "query", None, "methodology"), "knowledge_qa"),
    (RouteKey("general", "chat", None, None), "general"),
)

#: 每个 domain 的代表组合与其落点。
DOMAIN_REPRESENTATIVES: tuple[tuple[str, Mapping[str, Any], Intent], ...] = (
    (
        "workout_execution 的日程查询",
        {
            "domain": "workout_execution",
            "action": "query",
            "execution_type": "schedule_query",
        },
        "view_schedule",
    ),
    (
        "workout_execution 的表单记录",
        {
            "domain": "workout_execution",
            "action": "create",
            "execution_type": "form_record",
        },
        "form_record",
    ),
    (
        "workout_execution 的自然语言打卡",
        {
            "domain": "workout_execution",
            "action": "create",
            "execution_type": "natural_language_record",
        },
        "natural_language_record",
    ),
    (
        "plan_management 的生成计划",
        {"domain": "plan_management", "action": "create"},
        "generate_plan",
    ),
    (
        "plan_management 的调整计划",
        {"domain": "plan_management", "action": "modify"},
        "adjust_plan",
    ),
    (
        "analytics 的进步查询",
        {"domain": "analytics", "action": "query"},
        "view_progress",
    ),
    (
        "knowledge_qa 的动作技术问答",
        {
            "domain": "knowledge_qa",
            "action": "query",
            "knowledge_type": "exercise_technique",
            "exercise_name": "杠铃平板卧推",
        },
        "knowledge_qa",
    ),
    (
        "knowledge_qa 的方法论问答",
        {
            "domain": "knowledge_qa",
            "action": "query",
            "knowledge_type": "methodology",
        },
        "knowledge_qa",
    ),
    ("general 的一般对话", {"domain": "general", "action": "chat"}, "general"),
)

#: 非法组合与其拒绝理由：组合表外、参数错位、取值域外与多余字段。
ILLEGAL_ROUTES: tuple[tuple[str, Mapping[str, Any]], ...] = (
    ("计划版本查询不在能力范围内", {"domain": "plan_management", "action": "query"}),
    (
        "删除动作已从取值域移除",
        {"domain": "workout_execution", "action": "delete"},
    ),
    ("analytics 不接受 create", {"domain": "analytics", "action": "create"}),
    ("general 只接受 chat", {"domain": "general", "action": "query"}),
    (
        "general 不得携带动作名",
        {"domain": "general", "action": "chat", "exercise_name": "杠铃背蹲"},
    ),
    (
        "workout_execution 不接受 modify",
        {"domain": "workout_execution", "action": "modify"},
    ),
    (
        "execution_type 只属于 workout_execution",
        {
            "domain": "plan_management",
            "action": "create",
            "execution_type": "form_record",
        },
    ),
    (
        "knowledge_type 只属于 knowledge_qa",
        {"domain": "analytics", "action": "query", "knowledge_type": "methodology"},
    ),
    (
        "exercise_name 只属于 knowledge_qa",
        {
            "domain": "workout_execution",
            "action": "create",
            "execution_type": "form_record",
            "exercise_name": "杠铃背蹲",
        },
    ),
    (
        "schedule_query 不再接受日期槽",
        {
            "domain": "workout_execution",
            "action": "query",
            "execution_type": "schedule_query",
            "schedule_day": "today",
        },
    ),
    (
        "exercise_technique 必须给出 exercise_name",
        {
            "domain": "knowledge_qa",
            "action": "query",
            "knowledge_type": "exercise_technique",
        },
    ),
    (
        "methodology 不得给出 exercise_name",
        {
            "domain": "knowledge_qa",
            "action": "query",
            "knowledge_type": "methodology",
            "exercise_name": "杠铃背蹲",
        },
    ),
    ("空白动作名不算给出", {
        "domain": "knowledge_qa",
        "action": "query",
        "knowledge_type": "exercise_technique",
        "exercise_name": "   ",
    }),
    ("未知 domain 取值", {"domain": "nutrition", "action": "query"}),
    ("多余字段", {"domain": "general", "action": "chat", "confidence": 0.9}),
    ("缺少 action", {"domain": "general"}),
)


@dataclass
class RouterGateway:
    """固定替身模型入口：只脚本化路由的结构化调用，文本调用即测试失败。"""

    responses: list[Mapping[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def text(self, system_prompt: str, user_payload: str) -> str:
        raise AssertionError("路由不得使用文本模型调用")

    async def structured(
        self,
        system_prompt: str,
        user_payload: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        self.calls.append((system_prompt, user_payload))
        return schema.model_validate(self.responses.pop(0))


def test_legal_combination_table_is_the_frozen_matrix() -> None:
    """组合表整表相等，且每个落点都是 ``graph.state.Intent`` 的成员。"""
    assert dict(WORKFLOW_INTENTS) == dict(LEGAL_ROUTES)
    for intent in WORKFLOW_INTENTS.values():
        assert intent in get_args(Intent)


@pytest.mark.parametrize("label, fields, expected", DOMAIN_REPRESENTATIVES)
def test_each_domain_representative_maps_to_its_branch(
    label: str, fields: Mapping[str, Any], expected: Intent
) -> None:
    """每个 domain 的代表组合都经唯一映射函数进入既有或新增的工作流分支。"""
    assert workflow_intent(FitnessIntent(**fields)) == expected


@pytest.mark.parametrize("label, fields", ILLEGAL_ROUTES)
def test_illegal_combinations_are_rejected_at_parse_time(
    label: str, fields: Mapping[str, Any]
) -> None:
    """组合表外、参数错位与取值域外的组合都是 ``ValidationError``，不落到默认分支。"""
    with pytest.raises(ValidationError):
        FitnessIntent(**fields)


def test_every_literal_combination_either_maps_or_is_rejected() -> None:
    """可穷尽性：枚举全部判别字段取值，命中组合表的能映射，表外组合全部被拒绝。"""
    mapped: set[RouteKey] = set()
    for domain, action, execution_type, knowledge_type in product(
        get_args(Domain),
        get_args(Action),
        (*get_args(ExecutionType), None),
        (*get_args(KnowledgeType), None),
    ):
        fields: dict[str, Any] = {
            "domain": domain,
            "action": action,
            "execution_type": execution_type,
            "knowledge_type": knowledge_type,
            "exercise_name": (
                "杠铃平板卧推" if knowledge_type == "exercise_technique" else None
            ),
        }
        try:
            FitnessIntent(**fields)
        except ValidationError:
            continue
        key = RouteKey(domain, action, execution_type, knowledge_type)
        mapped.add(key)

    assert mapped == set(WORKFLOW_INTENTS)


#: 相对日期、星期与月历问法的代表请求：路由只给 schedule_query，日期由工具路径的模型解释。
SCHEDULE_PHRASINGS: tuple[str, ...] = (
    "今天练什么",
    "明天练什么",
    "后天练什么",
    "周五练什么",
    "这个月有哪些训练",
)


def test_schedule_query_needs_no_date_slot() -> None:
    """日期槽已从 Schema 移除：日程查询只需 domain／action／execution_type 三个判别字段。"""
    assert "schedule_day" not in FitnessIntent.model_fields

    scheduled = FitnessIntent(
        domain="workout_execution", action="query", execution_type="schedule_query"
    )

    assert workflow_intent(scheduled) == "view_schedule"


def test_history_and_recent_workout_questions_are_analytics_queries() -> None:
    """个人最佳、趋势、最近训练与历史训练内容都归 analytics/query 同一个只读工具分支。"""
    analytics = FitnessIntent(domain="analytics", action="query")

    assert workflow_intent(analytics) == "view_progress"


def test_exercise_name_is_normalized_to_text() -> None:
    """动作名去掉首尾空白；空白动作名按未给出处理。"""
    fitness = FitnessIntent(
        domain="knowledge_qa",
        action="query",
        knowledge_type="exercise_technique",
        exercise_name="  杠铃平板卧推  ",
    )

    assert fitness.exercise_name == "杠铃平板卧推"


def test_router_prompt_states_every_domain_action_and_the_schedule_scope() -> None:
    """业务提示词覆盖全部 domain／action 取值，并写明日程查询不再提取日期参数。"""
    for domain in get_args(Domain):
        assert domain in ROUTER_SYSTEM_PROMPT
    for action in get_args(Action):
        assert f"action={action}" in ROUTER_SYSTEM_PROMPT
    assert "schedule_query" in ROUTER_SYSTEM_PROMPT
    assert "训练日历" in ROUTER_SYSTEM_PROMPT
    assert "这个月" in ROUTER_SYSTEM_PROMPT
    assert "不提取日期参数" in ROUTER_SYSTEM_PROMPT
    assert "schedule_day" not in ROUTER_SYSTEM_PROMPT
    assert "active 计划" in ROUTER_SYSTEM_PROMPT
    assert "渐进式超负荷" in ROUTER_SYSTEM_PROMPT
    assert "分化思路" in ROUTER_SYSTEM_PROMPT


#: 五个字段的 Schema 描述必须覆盖的语义片段：字段名 → 描述须包含的关键词。
FIELD_DESCRIPTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("domain", ("领域",)),
    ("action", ("动作",)),
    ("execution_type", ("workout_execution", "必须为 null")),
    ("knowledge_type", ("knowledge_qa", "必须为 null")),
    ("exercise_name", ("knowledge_qa", "exercise_technique", "必须为 null")),
)


def test_schema_property_descriptions_state_field_semantics() -> None:
    """每个判别字段都带非空描述，并写明所属分支与非所属分支必须为 null。"""
    properties = FitnessIntent.model_json_schema()["properties"]

    assert set(properties) == {
        "domain",
        "action",
        "execution_type",
        "knowledge_type",
        "exercise_name",
    }
    for name, keywords in FIELD_DESCRIPTION_KEYWORDS:
        description = properties[name].get("description")
        assert description, f"{name} 缺少字段描述"
        for keyword in keywords:
            assert keyword in description, f"{name} 描述缺少 {keyword}"


def test_router_prompt_requires_every_field_with_null_placeholders() -> None:
    """提示词写明 strict Schema 要求全部字段出现，未用字段必须输出 null。"""
    assert "strict Schema" in ROUTER_SYSTEM_PROMPT
    assert "未用到的字段必须在输出里显式给出 null" in ROUTER_SYSTEM_PROMPT
    assert "不得省略" in ROUTER_SYSTEM_PROMPT


@pytest.mark.parametrize(
    "request_text", ["打开打卡表单", "查看进步", "生成新的训练计划", "你好"]
)
async def test_every_request_pays_one_structured_router_call(
    request_text: str,
) -> None:
    """每个请求都走一次结构化路由：既有封闭短语不再构成零调用的快路径。"""
    model = RouterGateway(
        responses=[
            {
                "domain": "general",
                "action": "chat",
            }
        ]
    )
    budget = ModelRequestBudget()

    fitness = await classify_intent(f"  {request_text}  ", model=model, budget=budget)

    assert fitness.domain == "general"
    assert len(model.calls) == 1
    assert model.calls[0][0] == ROUTER_SYSTEM_PROMPT
    assert model.calls[0][1] == dump_model_payload({"request": request_text})
    assert json.loads(model.calls[0][1]) == {"request": request_text}
    assert budget.used == 1


@pytest.mark.parametrize("request_text", SCHEDULE_PHRASINGS)
async def test_relative_and_calendar_phrasings_route_without_a_date_slot(
    request_text: str,
) -> None:
    """今天／明天／后天／星期／月历问法：路由载荷只有原始请求，落点只由 domain 与 execution_type 决定。"""
    model = RouterGateway(
        responses=[
            {
                "domain": "workout_execution",
                "action": "query",
                "execution_type": "schedule_query",
            }
        ]
    )

    fitness = await classify_intent(
        request_text, model=model, budget=ModelRequestBudget()
    )

    assert workflow_intent(fitness) == "view_schedule"
    assert model.calls[0][1] == dump_model_payload({"request": request_text})


async def test_history_question_routes_to_the_progress_branch() -> None:
    """“上次练了什么”属于 analytics/query：历史训练内容走进展工具分支。"""
    model = RouterGateway(responses=[{"domain": "analytics", "action": "query"}])

    fitness = await classify_intent(
        "我上次练了什么", model=model, budget=ModelRequestBudget()
    )

    assert workflow_intent(fitness) == "view_progress"
