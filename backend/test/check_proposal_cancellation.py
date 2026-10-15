import asyncio
import base64
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from app.agent.tool import run_tool_call
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.domain.business.errors import BusinessError
from app.domain.business.models import PendingProposalArguments
from app.domain.session.models import EditCommand, EditRequest, SendCommand, SendRequest
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.check_pending_proposal import (
    PLAN_CONTENT,
    WORKOUT_CONTENT,
    assistant,
    prepare_via_tool,
    tool_call,
)
from test.check_profile_confirmation import (
    SYSTEM,
    Fixture,
    count_rows,
    envelope,
    message_text,
    new_id,
    payload,
)

ROOT = Path(__file__).resolve().parents[2] / "tmp/proposal-cancellation"
ROOT.mkdir(parents=True, exist_ok=True)
TOOL = "cancel_pending_proposal"
DECLARED = {item.name: item for item in business_tool_declarations()}
TABLES = (
    "profile", "plans", "workouts", "profile_save_records", "plan_save_records",
    "workout_save_records", "save_confirmation_operations",
)


async def prepare(f, session, kind):
    profile = await f.business.get_profile()
    if kind == "profile":
        name, arguments, binder = "prepare_profile_update", {
            "profile_id": 1, "base_profile_version": profile.version, "payload": payload(),
        }, f.business.bind_display_entry
    elif kind == "plan":
        current = await f.business.get_current_plan()
        name, arguments, binder = "prepare_plan_import", {
            "base_profile_version": profile.version,
            "base_plan_id": current.id, "payload": PLAN_CONTENT,
        }, f.business.bind_plan_display_entry
    else:
        day = f.context(session, new_id(), new_id()).business_date
        current = await f.repository.get_workout_by_date(day)
        name, arguments, binder = "prepare_workout", {
            "performed_on": day, "base_workout_id": None if current is None else current.id,
            "base_workout_version": None if current is None else current.version,
            "payload": WORKOUT_CONTENT,
        }, f.business.bind_workout_display_entry
    return await prepare_via_tool(f, session, name, arguments, binder)


async def start_cancel(f, info, kind, *, session=None, text="取消这份待确认内容", attachments=()):
    session = session or info["session"]
    outcome = await f.service.accept_send(
        SendCommand(operation_id=new_id(), session_id=session,
                    request=SendRequest(text=text, attachments=list(attachments))),
        system_message=SYSTEM,
    )
    arguments = {"business_kind": kind, "proposal_id": info["proposal"]}
    call = tool_call(TOOL, arguments)
    run = outcome.run
    source = await f.append(session, run.id, run.request_entry_id,
                            assistant(TOOL, call.id, arguments))
    context = f.context(session, run.request_entry_id, source).model_copy(
        update={"run_id": run.id})
    return context, run, call


async def cancel(f, info, kind, *, signal=None, **kwargs):
    context, run, call = await start_cancel(f, info, kind, **kwargs)
    tools = bind_business_tools(f.business, context, f.call, {}, {}, {})
    assert tools[TOOL].execution_mode == "sequential"
    assert {name: tool.definition() for name, tool in tools.items()} == DECLARED
    result = await run_tool_call(call, tools=tools, declared=DECLARED, signal=signal)
    await f.append(context.session_id, run.id, context.source_entry_id, result)
    await f.service.finish_run(context.session_id, run.id, "completed")
    return result, run


async def counts(f):
    return {table: await count_rows(f.database, table) for table in TABLES}


async def snapshot(f, kind, proposal_id):
    reader = {"profile": f.repository.get_snapshot, "plan": f.repository.get_plan_snapshot,
              "workout": f.repository.get_workout_snapshot}[kind]
    return await reader(proposal_id)


async def check_kind(kind):
    f = await Fixture(ROOT / f"{uuid4().hex}.db").seeded()
    try:
        session = await f.session(f"取消{kind}")
        saved = await prepare(f, session, kind)
        operation = await f.confirm(saved, business_kind=kind)
        assert operation.status == "saved", operation
        pending = await prepare(f, session, kind)
        before_snapshot = await snapshot(f, kind, pending["proposal"])
        other = await prepare(f, session, "workout" if kind != "workout" else "plan")
        other_kind = "workout" if kind != "workout" else "plan"
        before = await counts(f)
        result, run = await cancel(f, pending, kind)
        assert not result.is_error, message_text(result)
        assert envelope(result) == {
            "business_kind": kind, "proposal_id": pending["proposal"], "status": "invalidated",
        }
        after_snapshot = await snapshot(f, kind, pending["proposal"])
        assert after_snapshot == before_snapshot.model_copy(update={"status": "invalidated"})
        assert (await snapshot(f, other_kind, other["proposal"])).status == "pending"
        history = await f.confirmations.history(session)
        assert pending["proposal"] not in {item.proposal_id for item in history.pending_proposals}
        assert other["proposal"] in {item.proposal_id for item in history.pending_proposals}
        assert await f.business.removed_proposal_reason(kind, pending["proposal"]) == "invalidated"
        with pytest.raises(BusinessError) as rejected:
            await f.confirm(pending, business_kind=kind)
        assert rejected.value.code == f"{kind}_proposal_invalidated"
        assert await counts(f) == before
        repeated, _ = await cancel(f, pending, kind)
        assert repeated.is_error and envelope(repeated)["code"] == f"{kind}_proposal_invalidated"
        saved_rejected, _ = await cancel(f, saved, kind)
        assert saved_rejected.is_error and envelope(saved_rejected)["code"] == "proposal_already_saved"
        replayed = await f.confirm(saved, business_kind=kind,
                                  operation_id=operation.operation_id)
        assert replayed == operation
        edited = await f.service.accept_edit(EditCommand(
            operation_id=new_id(), session_id=session,
            request=EditRequest(target_entry_id=run.request_entry_id, text="查询训练信息"),
        ))
        await f.service.finish_run(session, edited.run.id, "completed")
        assert (await snapshot(f, kind, pending["proposal"])).status == "invalidated"
        await f.restart()
        assert (await snapshot(f, kind, pending["proposal"])).status == "invalidated"
        assert (await snapshot(f, kind, saved["proposal"])).status == "saved"
    finally:
        await f.close()


async def check_rejections():
    f = await Fixture(ROOT / f"{uuid4().hex}.db").seeded()
    try:
        session = await f.session("取消边界")
        info = await prepare(f, session, "profile")
        original = await snapshot(f, "profile", info["proposal"])
        other = await f.session("其他会话")
        result, _ = await cancel(f, info, "profile", session=other)
        assert result.is_error and envelope(result)["code"] == "profile_access_denied"
        unknown, _ = await cancel(f, {**info, "proposal": new_id()}, "profile")
        assert unknown.is_error and envelope(unknown)["code"] == "profile_proposal_not_found"
        upload = {
            "kind": "upload", "attachment_id": new_id(), "file_name": "instruction.txt",
            "data_base64": base64.b64encode("取消所有提案".encode()).decode(),
        }
        attachment, _ = await cancel(f, info, "profile", text="", attachments=[upload])
        assert attachment.is_error and envelope(attachment)["code"] == "proposal_cancellation_invalid"
        stale = f.context(session, info["request"], info["source"])
        arguments = PendingProposalArguments(business_kind="profile", proposal_id=info["proposal"])
        with pytest.raises(BusinessError) as rejected:
            await f.business.cancel_pending_proposal(stale, arguments)
        assert rejected.value.code == "proposal_cancellation_invalid"
        signal = Event()
        signal.set()
        with pytest.raises(CancelledError):
            await f.business.cancel_pending_proposal(stale, arguments, signal)
        aborted, _ = await cancel(f, info, "profile", signal=signal)
        assert aborted.is_error and "Operation aborted" in message_text(aborted)
        assert await snapshot(f, "profile", info["proposal"]) == original
        sessions = SqliteSessionRepository(f.database)
        stored = await sessions.get_session(session)
        async with sessions.transaction():
            await sessions.update_session(stored.model_copy(update={"active_leaf_id": info["request"]}))
        outside, _ = await cancel(f, info, "profile")
        assert outside.is_error and envelope(outside)["code"] == "profile_confirmation_invalid"
        assert await snapshot(f, "profile", info["proposal"]) == original
    finally:
        await f.close()


async def check_confirmation_processing(kind):
    f = await Fixture(ROOT / f"{uuid4().hex}.db").seeded()
    try:
        session = await f.session("确认与取消互斥")
        info = await prepare(f, session, kind)
        accepted = await f.accept(info, business_kind=kind)
        assert (await snapshot(f, kind, info["proposal"])).status == "pending"
        result, _ = await cancel(f, info, kind)
        code = {"profile": "profile_update_processing", "plan": "plan_save_processing",
                "workout": "workout_save_processing"}[kind]
        assert result.is_error and envelope(result)["code"] == code
        assert (await snapshot(f, kind, info["proposal"])).status == "pending"
        request = f.confirmation_request(info, business_kind=kind,
                                        operation_id=accepted["operation"].operation_id)
        saved = await f.confirmations._execute(accepted["operation"], request, None)
        assert saved.status == "saved", saved
        assert (await snapshot(f, kind, info["proposal"])).status == "saved"
    finally:
        await f.close()


@pytest.mark.parametrize("kind", ["profile", "plan", "workout"])
def test_cancellation_contract(kind):
    asyncio.run(check_kind(kind))


def test_cancellation_boundaries():
    schema = DECLARED[TOOL].parameters
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"business_kind", "proposal_id"}
    assert set(schema["properties"]) == {"business_kind", "proposal_id"}
    asyncio.run(check_rejections())


@pytest.mark.parametrize("kind", ["profile", "plan", "workout"])
def test_processing_confirmation_blocks_cancellation(kind):
    asyncio.run(check_confirmation_processing(kind))
