# ST-01 契约层回归：Run 初始状态与 revision_count 边界、终态字面量改名、Tool 执行上下文与
# ToolResult／SkillBundle 的最小封装。
# 依据：docs/subtask/ST-01-state-contracts-and-tool-context.md「验收标准」。

from dataclasses import FrozenInstanceError, fields
from datetime import date
from typing import get_args, get_type_hints

import pytest
from pydantic import ValidationError

from app.application.agent.contracts import (
    TerminationReason,
    ToolEvidence,
    ToolExecutionContext,
    ToolResult,
    WorkflowState,
    initial_workflow_state,
)

BUSINESS_DAY = date(2026, 6, 1)


def _initial_state() -> WorkflowState:
    return initial_workflow_state(
        run_id="run-1",
        conversation_id="conv-1",
        user_id="user-1",
        client_request_id="request-1",
        business_day=BUSINESS_DAY,
        request="今天练什么？",
    )


def test_initial_state_starts_at_revision_count_zero() -> None:
    """初始状态：``revision_count`` 为 0，候选、确认与终态全部留空，``messages`` 尚未产生。"""
    state = _initial_state()

    assert state.get("revision_count") == 0
    assert state.get("plan_draft") is None
    assert state.get("evaluation_result") is None
    assert state.get("revision_feedback") == ()
    assert state.get("safety_hits") == ()
    assert state.get("draft_plan_id") is None
    assert state.get("confirmation") is None
    assert state.get("termination_reason") is None
    assert state.get("final_result") is None
    assert "messages" not in state


def test_initial_state_carries_the_injected_run_identity() -> None:
    """Run、会话、用户、请求身份与业务日由 API 注入，逐项落到初始状态。"""
    state = _initial_state()

    assert state.get("run_id") == "run-1"
    assert state.get("conversation_id") == "conv-1"
    assert state.get("user_id") == "user-1"
    assert state.get("client_request_id") == "request-1"
    assert state.get("business_day") == BUSINESS_DAY
    assert state.get("request") == "今天练什么？"
    assert state.get("intent") is None


def test_revision_count_is_declared_as_zero_or_one() -> None:
    """修订次数只有 0 与 1 两个取值：类型层就排除第二次回环。"""
    hint = get_type_hints(WorkflowState, include_extras=True)["revision_count"]

    assert get_args(hint) == (0, 1)


def test_failed_candidate_is_the_only_discard_termination_literal() -> None:
    """终态字面量整表冻结：二次评审失败用 ``discard_failed_candidate``，旧字面量不再存在。"""
    assert set(get_args(TerminationReason)) == {
        "safety_stop",
        "archive_draft",
        "discard_failed_candidate",
    }


def test_tool_execution_context_is_the_frozen_revision_snapshot() -> None:
    """ToolExecutionContext 是同一 Run 复用的一份事实快照键：字段固定且不可变。"""
    context = ToolExecutionContext(
        user_id="user-1",
        run_id="run-1",
        business_day=BUSINESS_DAY,
        profile_revision=3,
        workouts_revision=5,
        plans_revision=2,
        catalog_revision=7,
    )

    assert [field.name for field in fields(context)] == [
        "user_id",
        "run_id",
        "business_day",
        "profile_revision",
        "workouts_revision",
        "plans_revision",
        "catalog_revision",
    ]
    with pytest.raises(FrozenInstanceError):
        setattr(context, "plans_revision", 99)


def test_tool_result_carries_the_revision_it_read() -> None:
    """载荷与 evidence 一起返回；``revision_domain`` 只接受四个事实域。"""
    result = ToolResult[dict[str, int]](
        data={"recent_sessions": 3},
        evidence=(
            ToolEvidence(
                tool_name="read_recent_workouts", revision_domain="workouts", revision=5
            ),
        ),
    )

    assert result.data == {"recent_sessions": 3}
    assert result.evidence[0].revision == 5

    with pytest.raises(ValidationError):
        ToolEvidence.model_validate(
            {"tool_name": "read_progress", "revision_domain": "metrics", "revision": 5}
        )
    with pytest.raises(ValidationError):
        ToolResult[dict[str, int]].model_validate(
            {"data": {}, "evidence": [], "unexpected": 1}
        )
