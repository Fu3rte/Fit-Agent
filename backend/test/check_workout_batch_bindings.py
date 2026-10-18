import asyncio
import json
from uuid import uuid4

import pytest

from app.ai.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from app.domain.business.errors import (
    BusinessError,
    ProfileConfirmationInvalid,
    WorkoutConfirmationInvalid,
)
from app.domain.business.models import (
    ProfileProposalArguments,
    WorkoutContent,
    WorkoutProposalArguments,
)
from app.domain.session.errors import SessionConflict
from app.domain.session.models import SendCommand, SendRequest
from test.check_profile_confirmation import (
    SYSTEM,
    USAGE,
    Fixture,
    count_rows,
    new_id,
    now_ms,
    payload,
)
from test.check_workout_service_http import CONTENT
from test.regression_support import temporary_root

ROOT = temporary_root("workout-batch-bindings") / uuid4().hex
ROOT.mkdir()


def result(call, body, error=False):
    return ToolResultMessage(role="toolResult", tool_call_id=call.id, tool_name=call.name,
        content=[TextContent(type="text", text=json.dumps(body))], is_error=error, timestamp=now_ms())


def assistant(content):
    return AssistantMessage(role="assistant", content=content, api="openai-completions",
        provider="example", model="model-1", usage=USAGE, stop_reason="toolUse", timestamp=now_ms())


async def batch(f, order, mutation=None):
    session = await f.session("真实批次结果路径")
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="整理画像和实际训练")), system_message=SYSTEM)
    run = outcome.run
    calls = [ToolCall(type="toolCall", id=new_id(), name=name, arguments={}) for name in order]
    source = await f.append(session, run.id, run.request_entry_id, assistant(calls))
    context = f.context(session, run.request_entry_id, source)
    proposals = {}
    displays = {}
    parent = source
    for index, call in enumerate(calls):
        if call.name == "prepare_profile_update":
            proposal = await f.business.profile.prepare_profile_update(context,
                ProfileProposalArguments(profile_id=1, base_profile_version=None, payload=payload()))
            proposals[call.name] = proposal
            message = result(call, proposal.model_dump())
        elif call.name == "prepare_workout":
            proposal = await f.business.workouts.prepare_workout(context, WorkoutProposalArguments(
                performed_on=context.business_date, base_workout_id=None, base_workout_version=None,
                payload=WorkoutContent.model_validate(CONTENT)))
            proposals[call.name] = proposal
            message = result(call, proposal.model_dump())
        elif call.name == "get_workout":
            with pytest.raises(BusinessError) as caught:
                await f.business.workouts.get_workout(new_id())
            message = result(call, caught.value.detail(), True)
        else:
            message = result(call, (await f.business.profile.get_profile()).model_dump())
        if mutation == "failed_prepare" and call.name in {"prepare_workout", "prepare_profile_update"}:
            message = message.model_copy(update={"is_error": True})
        if index == 1 and mutation is not None:
            if mutation == "duplicate":
                message = result(calls[0], {})
            elif mutation == "foreign":
                message = message.model_copy(update={"tool_call_id": new_id()})
            elif mutation == "wrong_name":
                message = message.model_copy(update={"tool_name": "get_profile"})
            elif mutation == "new_batch":
                message = assistant([TextContent(type="text", text="新的助手批次")])
            elif mutation == "user":
                message = UserMessage(role="user", content="新的用户消息", timestamp=now_ms())
            elif mutation == "missing":
                continue
        parent = await f.append(session, run.id, parent, message)
        displays[call.name] = parent
        if call.name == "prepare_profile_update":
            await f.business.profile.bind_display_entry(proposal.proposal_id, parent)
    return session, run, context, proposals, displays


async def valid(order):
    f = await Fixture(ROOT / (uuid4().hex + ".db")).seeded()
    try:
        session, run, context, proposals, displays = await batch(f, order)
        workout = proposals["prepare_workout"]
        await f.business.workouts.bind_workout_display_entry(workout.proposal_id, displays["prepare_workout"])
        await f.service.runs.finish_run(session, run.id, "completed")
        # 合法保存只经独立确认入口：同一批次的两份快照分别由各自按钮确认。
        workout_op = await f.confirm(
            {"session": session, "proposal": workout.proposal_id,
             "display": displays["prepare_workout"]}, business_kind="workout")
        assert workout_op.status == "saved", workout_op
        profile = proposals["prepare_profile_update"]
        profile_op = await f.confirm(
            {"session": session, "proposal": profile.proposal_id,
             "display": displays["prepare_profile_update"]}, business_kind="profile")
        assert profile_op.status == "saved", profile_op
        assert workout_op.result.version == profile_op.result.version == 1
        return {"order": order, "workout_version": 1, "profile_version": 1}
    finally:
        await f.close()


async def invalid(mutation):
    f = await Fixture(ROOT / (uuid4().hex + ".db")).seeded()
    try:
        if mutation == "user":
            with pytest.raises(SessionConflict):
                await batch(f, ["get_profile", "get_workout", "prepare_workout"], mutation)
            return "user:session_conflict"
        # 非法批次结果路径在展示绑定阶段就地拒绝：配对、角色与完整内容核验先于保存，零业务写入。
        order = ["get_profile", "get_workout", "prepare_workout", "prepare_profile_update"]
        with pytest.raises((ProfileConfirmationInvalid, WorkoutConfirmationInvalid)):
            session, run, context, proposals, displays = await batch(f, order, mutation)
            await f.business.workouts.bind_workout_display_entry(
                proposals["prepare_workout"].proposal_id, displays["prepare_workout"])
        assert await count_rows(f.database, "workout_save_records") == 0
        assert await count_rows(f.database, "profile_save_records") == 0
        return mutation
    finally:
        await f.close()


def check():
    evidence = {"valid": [], "rejected": []}
    for order in [
        ["get_profile", "prepare_workout", "prepare_profile_update"],
        ["get_profile", "prepare_profile_update", "prepare_workout"],
        ["get_profile", "get_workout", "prepare_workout", "prepare_profile_update"],
    ]:
        evidence["valid"].append(asyncio.run(valid(order)))
    for mutation in ["duplicate", "foreign", "wrong_name", "failed_prepare", "new_batch", "user", "missing"]:
        evidence["rejected"].append(asyncio.run(invalid(mutation)))
    (ROOT / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print("PASS: real mixed profile/workout batch display positions 2/3/4; failed intermediate result; duplicate/foreign/cross-batch/missing result rejection:", ROOT)


if __name__ == "__main__":
    check()
