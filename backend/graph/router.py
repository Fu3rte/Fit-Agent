"""Stage 5 §3.6 确定性 Intent Router：封闭短语高置信分类 ＋ 零／多命中时的一次严格枚举兜底。

依据：``refactor-log/stage5.md`` §3.6／§3.9／§4.3；``Fit-Agent-LangGraph-重构讨论总结.md`` §4.1／§4.3；
``LANGGRAPH_REFACTOR_PLAN.md`` §9.1。

四条边界：

- **Router 是函数，不是第三个 Agent**（§3.6 前言）：没有独立 Agent、A2A 或插件注册中心；返回值只是
  冻结词表 ``graph.state.INTENTS`` 里的一个 ``intent``，分派与预算由
  ``graph/workflow.py::invoke_agent_run`` 负责。
- **封闭词表**：请求先 ``strip``，英文匹配忽略大小写（``casefold``）；只做精确子串高置信分类，不做
  评分、分词、同义词扩展或数值阈值，也不新增词条。
- **零命中或多 intent 命中才调一次模型**：模型只接收当前请求与五类定义，严格输出
  ``{"intent": <五类枚举>}``；额外字段或非法枚举都是运行错误，不猜默认值。分类与后续计划链路共享
  同一 ``graph.nodes.ModelRequestBudget``（最坏 1＋4 次仍受每 Run 5 次上限约束）。
- **不做安全判定**：急性关键词与 ``safety_stop`` 由计划子图的 ``safety_check`` 节点决定，本模块不
  扩词表、不诊断，也不读库、不写库。
"""

import asyncio

from pydantic import BaseModel, ConfigDict

from graph.model import ModelCall, parse_model_json
from graph.nodes import ModelRequestBudget
from graph.state import Intent

#: §3.6 封闭词表：``form_record`` 的确定性命中短语。
FORM_RECORD_PHRASES: tuple[str, ...] = ("打开打卡表单", "使用表单记录", "表单打卡")

#: §3.6 ``natural_language_record`` 的记录动词；必须与事实标记同时出现才算命中。
NATURAL_LANGUAGE_RECORD_VERBS: tuple[str, ...] = ("记录", "打卡", "练了", "完成了")

#: §3.6 ``natural_language_record`` 的事实标记。
NATURAL_LANGUAGE_RECORD_FACT_MARKERS: tuple[str, ...] = (
    "kg",
    "公斤",
    "次",
    "组",
    "秒",
    "今天",
    "昨天",
)

#: §3.6 ``view_progress`` 的确定性命中短语。
VIEW_PROGRESS_PHRASES: tuple[str, ...] = (
    "查看进步",
    "训练进展",
    "最近表现",
    "个人最佳",
    "PB",
    "趋势",
    "看板",
)

#: §3.6 ``generate_plan`` 的确定性命中短语。
GENERATE_PLAN_PHRASES: tuple[str, ...] = (
    "生成计划",
    "制定计划",
    "新训练计划",
    "做个训练计划",
)

#: §3.6 ``adjust_plan`` 的确定性命中短语。
ADJUST_PLAN_PHRASES: tuple[str, ...] = (
    "调整计划",
    "修改计划",
    "改计划",
    "调整训练安排",
)

#: 提示词里给模型的五类定义：与上面的封闭短语表同一份来源，不另写一份词表。
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

#: Router 兜底分类的系统提示词：五类定义与严格输出形状都在这里给出（不当成业务指令）。
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
    """Router 兜底分类的严格输出：只有 ``intent`` 一个字段，额外字段与非法枚举都拒绝。"""

    model_config = ConfigDict(extra="forbid")

    intent: Intent


def deterministic_intents(request: str) -> tuple[Intent, ...]:
    """封闭词表命中的 intent，顺序即 ``graph.state.INTENTS`` 的词表顺序；空元组即零命中。

    零命中与多 intent 命中一样都交给 :func:`classify_intent` 的一次模型分类：本函数不替模型挑一个，
    也不做评分、优先级排序或同义词扩展。
    """
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
    """五类 intent 的唯一入口：单一确定性命中直接返回，零／多命中才调一次模型分类。

    模型请求先经 ``budget`` 扣预算（与后续计划链路共享同一份 Run 预算）；超时、调用失败与非法响应
    都向上抛，作为运行错误终止本次 Run，不写任何业务行、不猜默认 intent。
    """
    hits = deterministic_intents(request)
    if len(hits) == 1:
        return hits[0]
    timeout = budget.begin_request()
    async with asyncio.timeout(timeout):
        text = await model(ROUTER_SYSTEM_PROMPT, request.strip())
    return parse_model_json(text, RouterClassification).intent


def _normalized(request: str) -> str:
    """分类用的规范化文本：先 ``trim``，再 ``casefold`` 让英文匹配忽略大小写（中文不受影响）。"""
    return request.strip().casefold()


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    """封闭短语命中判定：精确子串包含，不做分词、不扩词。"""
    return any(phrase.casefold() in text for phrase in phrases)
