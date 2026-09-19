"""确定性 Intent Router：封闭短语高置信分类 ＋ 零／多命中时的一次严格枚举兜底。"""

import asyncio

from pydantic import BaseModel, ConfigDict

from graph.model import ModelCall, parse_model_json
from graph.nodes import ModelRequestBudget
from graph.state import Intent

FORM_RECORD_PHRASES: tuple[str, ...] = ("打开打卡表单", "使用表单记录", "表单打卡")

NATURAL_LANGUAGE_RECORD_VERBS: tuple[str, ...] = ("记录", "打卡", "练了", "完成了")

NATURAL_LANGUAGE_RECORD_FACT_MARKERS: tuple[str, ...] = (
    "kg",
    "公斤",
    "次",
    "组",
    "秒",
    "今天",
    "昨天",
)

VIEW_PROGRESS_PHRASES: tuple[str, ...] = (
    "查看进步",
    "训练进展",
    "最近表现",
    "个人最佳",
    "PB",
    "趋势",
    "看板",
)

GENERATE_PLAN_PHRASES: tuple[str, ...] = (
    "生成计划",
    "制定计划",
    "新训练计划",
    "做个训练计划",
)

ADJUST_PLAN_PHRASES: tuple[str, ...] = (
    "调整计划",
    "修改计划",
    "改计划",
    "调整训练安排",
)

ROUTER_INTENT_DEFINITIONS: tuple[tuple[Intent, str], ...] = (
    ("form_record", "用户要用打卡表单记录训练：" + "／".join(FORM_RECORD_PHRASES)),
    (
        "natural_language_record",
        "用户用自然语言报告一次训练事实，同时出现记录动词（"
        + "／".join(NATURAL_LANGUAGE_RECORD_VERBS)
        + "）与事实标记（"
        + "／".join(NATURAL_LANGUAGE_RECORD_FACT_MARKERS)
        + "）",
    ),
    (
        "view_progress",
        "用户要查看训练进展、个人最佳或趋势：" + "／".join(VIEW_PROGRESS_PHRASES),
    ),
    ("generate_plan", "用户要生成一份新的训练计划：" + "／".join(GENERATE_PLAN_PHRASES)),
    ("adjust_plan", "用户要调整已有的训练安排：" + "／".join(ADJUST_PLAN_PHRASES)),
)

ROUTER_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的意图路由器，只做一次分类：不执行任何业务动作、不写库、不生成或修改计划、"
    "不做安全判定。从下列五类 intent 中选出唯一一个，只输出一个 JSON 对象，字段与取值严格如下，"
    '不输出解释文字、额外字段或其它取值：{"intent": "<五类之一>"}。\n'
    "五类定义：\n"
    + "\n".join(
        f"- {intent}：{definition}"
        for intent, definition in ROUTER_INTENT_DEFINITIONS
    )
)


class RouterClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent


def deterministic_intents(request: str) -> tuple[Intent, ...]:
    """封闭词表命中的 intent，按五类固定顺序返回；空元组即零命中。"""
    text = _normalized(request)
    hits: list[Intent] = []
    if _contains_any(text, FORM_RECORD_PHRASES):
        hits.append("form_record")
    if _contains_any(text, NATURAL_LANGUAGE_RECORD_VERBS) and _contains_any(
        text, NATURAL_LANGUAGE_RECORD_FACT_MARKERS
    ):
        hits.append("natural_language_record")
    if _contains_any(text, VIEW_PROGRESS_PHRASES):
        hits.append("view_progress")
    if _contains_any(text, GENERATE_PLAN_PHRASES):
        hits.append("generate_plan")
    if _contains_any(text, ADJUST_PLAN_PHRASES):
        hits.append("adjust_plan")
    return tuple(hits)


async def classify_intent(
    request: str, *, model: ModelCall, budget: ModelRequestBudget
) -> Intent:
    """五类 intent 的唯一入口：单一确定性命中直接返回，零／多命中才调一次模型分类。"""
    hits = deterministic_intents(request)
    if len(hits) == 1:
        return hits[0]
    timeout = budget.begin_request()
    async with asyncio.timeout(timeout):
        text = await model(ROUTER_SYSTEM_PROMPT, request.strip())
    return parse_model_json(text, RouterClassification).intent


def _normalized(request: str) -> str:
    return request.strip().casefold()


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase.casefold() in text for phrase in phrases)
