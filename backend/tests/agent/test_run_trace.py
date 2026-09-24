from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolRuntime

from app.application.agent.budget import ModelRequestBudget, ToolCallBudgetExceeded
from app.application.agent.harness.declaration import HarnessContext
from app.application.agent.harness.policy import build_tool_call_wrapper
from app.application.agent.harness.snapshot import MissingPlanFacts
from app.application.agent.harness.tools.exercise_dataset.tools import search_exercises
from app.application.agent.run_service import RunTraceRecorder, persisted_events
from app.application.ports import ModelGateway
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.conversations_repository import (
    ConversationRepo,
)


async def _text(_system: str, _prompt: str) -> str:
    raise AssertionError("预算拒绝时不应调用模型")


async def _structured(
    _system: str, _prompt: str, _schema: type[Any]
) -> Any:
    raise AssertionError("预算拒绝时不应调用模型")


async def _tools(
    _messages: Sequence[BaseMessage], _offered: Sequence[Any]
) -> AIMessage:
    raise AssertionError("预算拒绝时不应调用模型")


async def test_budget_rejection_traces_the_proposed_ninth_tool() -> None:
    budget = ModelRequestBudget(max_tool_calls=8)
    for _ in range(8):
        budget.take_tool_call()
    recorded: list[tuple[str, str, str, str, str | None]] = []

    async def record(
        stage: str,
        call_id: str,
        tool_name: str,
        status: str,
        error_code: str | None,
    ) -> None:
        recorded.append((stage, call_id, tool_name, status, error_code))

    context = HarnessContext(
        model=ModelGateway(text=_text, structured=_structured, tools=_tools),
        budget=budget,
        trace_call=record,
    )
    runtime = ToolRuntime(
        state={"messages": []},
        context=context,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id="requested-ninth",
        store=None,
    )
    request = ToolCallRequest(
        tool_call={"id": "requested-ninth", "name": "search_exercises", "args": {}},
        tool=search_exercises,
        state={"messages": []},
        runtime=runtime,
    )
    wrapper = build_tool_call_wrapper(timeout_seconds=1)

    async def execute(_request: ToolCallRequest) -> Any:
        raise AssertionError("预算拒绝后不能执行工具")

    with pytest.raises(ToolCallBudgetExceeded):
        await wrapper(request, execute)

    assert recorded == [
        (
            "general",
            "requested-ninth",
            "search_exercises",
            "failure",
            "ToolCallBudgetExceeded",
        )
    ]


async def test_missing_plan_facts_keeps_completed_calls_and_records_run_failure(
    tmp_path: Any,
) -> None:
    db = Database(tmp_path / "trace.db")
    await db.open()
    try:
        await db.migrate()
        conversations = ConversationRepo(db)
        conversation = await conversations.create_conversation(
            conversation_id="chat", title="trace", created_at="2026-06-01T09:00:00+00:00"
        )
        async with db.transaction() as conn:
            run, _entry = await conversations.begin_run_in_transaction(
                conn,
                conversation_id=conversation.id,
                run_id="run",
                thread_id="thread",
                client_request_id="request",
                entry_id="entry",
                content="request",
                created_at="2026-06-01T09:00:00+00:00",
            )
        recorder = RunTraceRecorder(
            conversations, run.id, now=lambda: "2026-06-01T09:01:00+00:00"
        )
        await recorder.record(
            "planning_tools", "call-1", "read_user_profile", "success", None
        )

        async def failed_events() -> AsyncIterator[Any]:
            raise MissingPlanFacts("missing required facts")
            yield

        result = [
            event
            async for event in persisted_events(
                db,
                conversations,
                run.id,
                failed_events(),
                now=lambda: "2026-06-01T09:02:00+00:00",
                trace_recorder=recorder,
            )
        ]
        entries = await conversations.list_run_trace(run.id)
        saved_run = await conversations.read_run(run.id)
        assert [(item["status"], item["error_code"]) for item in entries] == [
            ("success", None),
            ("failure", "MissingPlanFacts"),
        ]
        assert entries[0]["tool_call_id"] == "call-1"
        assert entries[1]["stage"] == "planning_tools"
        assert entries[1]["tool_call_id"] is None and entries[1]["tool_name"] is None
        assert saved_run is not None and saved_run.error_code == "MissingPlanFacts"
        assert [event.event for event in result] == ["error"]
    finally:
        await db.close()
