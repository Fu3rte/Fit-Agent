"""Stage 3 子任务 01：Graph State 契约（字段冻结、词汇表、LangGraph 可用性）。

依据：``refactor-log/stage3.md`` §3；讨论总结 §4.1／§4.2／§5.1；REFACTOR_PLAN §8.1／§9.1／§9.2。

本文件只验证 State 契约本身：不搭业务节点、不接 Checkpointer、不调模型。
"""

from typing import get_args

from langgraph.graph import END, START, StateGraph

from graph.state import (
    INTENTS,
    ConfirmationStatus,
    Intent,
    TerminationReason,
    WorkflowState,
)

#: REFACTOR_PLAN §8.1 的九类内容 + stage3.md §3 的 draft 身份／内容拆分。
#: 该集合即冻结契约：新增字段必须先改权威文档，本断言随之失败。
FROZEN_STATE_FIELDS = {
    "conversation_id",
    "request",
    "intent",
    "context",
    "loaded_skill",
    "draft_plan_id",
    "draft_plan",
    "evaluation",
    "revision_count",
    "confirmation",
    "termination_reason",
}


def test_state_fields_are_frozen_to_the_authoritative_contract() -> None:
    """字段集合精确等于权威列表：既不加 API Key／Provider 字段，也不加统计缓存字段或第二套线程身份。"""
    assert set(WorkflowState.__annotations__) == FROZEN_STATE_FIELDS
    # total=False：节点与恢复路径允许缺省字段，不要求全量 State。
    assert set(WorkflowState.__optional_keys__) == FROZEN_STATE_FIELDS


def test_intent_vocabulary_matches_the_router_branches() -> None:
    """已判定 intent 恰为讨论总结 §4.1 的五条路由分支，常量与 Literal 同源不漂移。"""
    assert INTENTS == (
        "form_record",
        "natural_language_record",
        "view_progress",
        "generate_plan",
        "adjust_plan",
    )
    assert get_args(Intent) == INTENTS


def test_confirmation_and_termination_vocabularies_match_the_plan_subgraph() -> None:
    """确认状态与终止原因只转录讨论总结 §4.2 已冻结的分支，不新增状态。"""
    assert get_args(ConfirmationStatus) == ("pending", "confirmed", "rejected")
    assert get_args(TerminationReason) == ("safety_stop", "archive_draft", "reject_draft")


def test_langgraph_accepts_the_frozen_state_and_keeps_conversation_identity() -> None:
    """LangGraph 直接接受该 State：节点只回写自己的键，缺省键不补默认值，会话身份不被替换。"""
    graph = StateGraph(WorkflowState)
    graph.add_node(
        "advance", lambda state: {"revision_count": state["revision_count"] + 1}
    )
    graph.add_edge(START, "advance")
    graph.add_edge("advance", END)

    result = graph.compile().invoke(
        {
            "conversation_id": "conv-1",
            "request": "生成计划",
            "intent": "generate_plan",
            "revision_count": 0,
        }
    )

    assert result == {
        "conversation_id": "conv-1",
        "request": "生成计划",
        "intent": "generate_plan",
        "revision_count": 1,
    }
