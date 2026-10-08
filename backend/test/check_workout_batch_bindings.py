import asyncio
import json
from uuid import uuid4

import pytest

from app.ai.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from app.domain.business.errors import BusinessError, ProfileConfirmationInvalid, WorkoutConfirmationInvalid
from app.domain.business.models import ProfileProposalArguments, ProfileSaveArguments, WorkoutContent, WorkoutProposalArguments, WorkoutSaveArguments
from app.domain.session.errors import SessionConflict
from app.domain.session.models import SendCommand, SendRequest
from test.check_profile_confirmation import Fixture, SYSTEM, USAGE, new_id, now_ms, payload
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
            proposal = await f.business.prepare_profile_update(context,
                ProfileProposalArguments(profile_id=1, base_profile_version=None, payload=payload()))
            proposals[call.name] = proposal
            message = result(call, proposal.model_dump())
        elif call.name == "prepare_workout":
            proposal = await f.business.prepare_workout(context, WorkoutProposalArguments(
                performed_on=context.business_date, base_workout_id=None, base_workout_version=None,
                payload=WorkoutContent.model_validate(CONTENT)))
            proposals[call.name] = proposal
            message = result(call, proposal.model_dump())
        elif call.name == "get_workout":
            with pytest.raises(BusinessError) as caught:
                await f.business.get_workout(new_id())
            message = result(call, caught.value.detail(), True)
        else:
            message = result(call, (await f.business.get_profile()).model_dump())
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
            await f.business.bind_display_entry(proposal.proposal_id, parent)
    return session, run, context, proposals, displays


async def confirm(f, session):
    outcome = await f.service.accept_send(SendCommand(operation_id=new_id(), session_id=session,
        request=SendRequest(text="确认指定内容")), system_message=SYSTEM)
    run = outcome.run
    source = await f.append(session, run.id, run.request_entry_id,
        assistant([TextContent(type="text", text="执行确认保存")]))
    await f.service.finish_run(session, run.id, "completed")
    return f.context(session, run.request_entry_id, source)


async def valid(order):
    f = await Fixture(ROOT / (uuid4().hex + ".db")).seeded()
    try:
        session, run, context, proposals, displays = await batch(f, order)
        workout = proposals["prepare_workout"]
        await f.business.bind_workout_display_entry(workout.proposal_id, displays["prepare_workout"])
        await f.service.finish_run(session, run.id, "completed")
        context = await confirm(f, session)
        saved = await f.business.save_workout(context, WorkoutSaveArguments(proposal_id=workout.proposal_id,
            display_entry_id=displays["prepare_workout"], confirmation_entry_id=context.request_entry_id))
        profile = proposals["prepare_profile_update"]
        context = await confirm(f, session)
        profile_saved = await f.business.save_profile_update(context, ProfileSaveArguments(
            proposal_id=profile.proposal_id, display_entry_id=displays["prepare_profile_update"],
            confirmation_entry_id=context.request_entry_id))
        assert saved.version == profile_saved.version == 1
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
        session, run, context, proposals, displays = await batch(f,
            ["get_profile", "get_workout", "prepare_workout", "prepare_profile_update"], mutation)
        with pytest.raises(WorkoutConfirmationInvalid):
            await f.business.bind_workout_display_entry(proposals["prepare_workout"].proposal_id,
                                                        displays["prepare_workout"])
        # 两业务共享检查按各自错误类别拒绝真实持久化的非法路径。
        entries = f.business._entries(await f.service.get_current_branch(session))
        with pytest.raises(ProfileConfirmationInvalid):
            f.business._require_prepare_result(entries[displays["prepare_profile_update"]],
                entries[context.source_entry_id], entries)
        assert await f.repository.get_workout_save_record(proposals["prepare_workout"].proposal_id) is None
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
