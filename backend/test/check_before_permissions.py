import asyncio
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from app.agent.permissions import create_before_tool_call
from app.agent.tool import (
    BeforeToolCallContext,
    CredentialDetectedError,
    PreparedToolCall,
    execute_prepared_tool_call,
    prepare_tool_call,
    run_tool_batch,
    run_tool_call,
)
from app.agent.tools.business import bind_business_tools
from app.agent.tools.files import check_file_permission, create_file_tools
from app.ai.messages import ToolCall
from app.domain.business.models import WorkoutListArguments
from test.check_business_profile import _regenerate
from test.check_plan_core import prepare as prepare_plan
from test.check_profile_confirmation import (
    DECLARED,
    Fixture,
    new_id,
    payload,
    prepare_arguments,
    propose,
)
from test.check_workout_service_http import prepare as prepare_workout
from test.regression_support import temporary_root, text


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def f():
    root = temporary_root("before-permissions") / uuid4().hex
    root.mkdir()
    fixture = await Fixture(root / "business.db").seeded()
    yield fixture
    await fixture.close()


def invocation(name, arguments):
    return ToolCall(type="toolCall", id=new_id(), name=name, arguments=arguments)


def declarations(tools):
    return {name: tool.definition() for name, tool in tools.items()}


def directory_link(link: Path, target: Path):
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode()
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_file_batch_boundaries(f, mode):
    root = f.path.parent / "files"
    session = new_id()
    tools = create_file_tools(session, tmp_root=root)
    tools = {name: replace(tool, execution_mode=mode) for name, tool in tools.items()}
    before = create_before_tool_call(f.business, f.call)
    prefix = f"sessions/{session}/workspace"
    allowed = root / prefix
    attachments = root / f"sessions/{session}/attachments"
    attachments.mkdir(parents=True)
    (attachments / "original.txt").write_text("original", encoding="utf-8")
    outside = f.path.parent / "outside"
    outside.mkdir()
    allowed.mkdir()
    directory_link(allowed / "escape", outside)
    (allowed / "parent-file").write_text("unchanged", encoding="utf-8")
    rejected = [
        f"sessions/{session}/attachments/original.txt",
        f"sessions/{new_id()}/workspace/new.txt",
        f"{prefix}/../attachments/new.txt",
        str(outside / "absolute.txt"),
        f"{prefix}/escape/new.txt",
    ]
    calls = [invocation("write", {"path": path, "content": "denied"}) for path in rejected]
    calls += [
        invocation("edit", {"path": rejected[0], "old_text": "original", "new_text": "denied"}),
        invocation("write", {"path": f"{prefix}/new/note.txt", "content": "one"}),
    ]
    after_ids = []

    async def after(context, signal):
        after_ids.append(context.tool_call.id)

    signal = Event()
    result = await run_tool_batch(
        calls, tools=tools, declared=declarations(tools), before_tool_call=before,
        after_tool_call=after, execution_mode=mode, signal=signal,
    )
    assert result.failure is None and not signal.is_set()
    assert [message.is_error for message in result.messages] == [True] * 6 + [False]
    assert after_ids == [calls[-1].id]
    assert (allowed / "new/note.txt").read_text(encoding="utf-8") == "one"
    assert (attachments / "original.txt").read_text(encoding="utf-8") == "original"
    assert list(outside.iterdir()) == []
    assert not (root / rejected[1]).exists()
    edit = await run_tool_call(
        invocation("edit", {"path": f"{prefix}/new/note.txt", "old_text": "one", "new_text": "two"}),
        tools=tools, declared=declarations(tools), before_tool_call=before,
    )
    assert not edit.is_error
    assert (allowed / "new/note.txt").read_text(encoding="utf-8") == "two"


@pytest.mark.anyio
async def test_readonly_precheck_and_execution_recheck(f):
    root = f.path.parent / "readonly"
    session = new_id()
    tools = create_file_tools(session, tmp_root=root)
    prefix = f"sessions/{session}/workspace"
    before = create_before_tool_call(f.business, f.call)
    write = invocation("write", {"path": f"{prefix}/missing/note.txt", "content": "safe"})
    outcome = await prepare_tool_call(
        0, write, tools=tools, declared=declarations(tools), before_tool_call=before,
    )
    assert isinstance(outcome, PreparedToolCall)
    assert not (root / "sessions").exists()
    assert write.arguments == {"path": f"{prefix}/missing/note.txt", "content": "safe"}
    outside = f.path.parent / "race-outside"
    outside.mkdir()
    (root / prefix).mkdir(parents=True)
    directory_link(root / prefix / "missing", outside)
    finalized = await execute_prepared_tool_call(outcome)
    assert finalized.message.is_error and "链接" in text(finalized.message)
    assert list(outside.iterdir()) == []

    other = new_id()
    other_tools = create_file_tools(other, tmp_root=root)
    denied = check_file_permission(BeforeToolCallContext(
        write, other_tools["write"].arguments.model_validate(write.arguments),
        other_tools["write"].trusted_context,
    ))
    assert denied.block
    missing_context = replace(tools["write"], trusted_context=None)
    failed = await run_tool_call(
        write, tools={"write": missing_context}, declared=declarations({"write": missing_context}),
        before_tool_call=before,
    )
    assert failed.is_error and "缺少可信" in text(failed)


async def business_case(f, kind):
    session = await f.session(kind)
    if kind == "save_profile_update":
        ids = await propose(f, session, prepare_arguments(None, payload()))
        return f.save_context(ids), f.save_arguments(ids), f.repository.get_snapshot
    if kind == "save_plan":
        ids = await propose(f, session, prepare_arguments(None, payload()))
        await f.service_save(ids)
        context, arguments = await prepare_plan(f, session)
        return context, arguments, f.repository.get_plan_snapshot
    day = f.context(session, new_id(), new_id()).business_date
    context, arguments, _ = await prepare_workout(f, session, day)
    if kind == "update_workout":
        record = await f.business.save_workout(context, arguments)
        context, arguments, _ = await prepare_workout(f, session, day, record)
    return context, arguments, f.repository.get_workout_snapshot


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["save_profile_update", "save_plan", "save_workout", "update_workout"])
async def test_business_authorization_and_idempotency(f, kind):
    context, arguments, get_snapshot = await business_case(f, kind)
    original_context = context.model_copy(deep=True)
    tools = bind_business_tools(f.business, context, f.call, {}, {}, {})
    assert declarations(tools) == DECLARED
    context.session_id = new_id()
    context.request_entry_id = new_id()
    context.source_entry_id = new_id()
    assert tools[kind].trusted_context.model_dump() == original_context.model_dump()
    before = create_before_tool_call(f.business, f.call)
    call = invocation(kind, arguments.model_dump())
    snapshot = await get_snapshot(arguments.proposal_id)
    changes = f.database.connection.total_changes
    checked = await prepare_tool_call(
        0, call, tools=tools, declared=DECLARED, before_tool_call=before,
    )
    assert isinstance(checked, PreparedToolCall)
    assert f.database.connection.total_changes == changes
    assert await get_snapshot(arguments.proposal_id) == snapshot
    assert snapshot.status == "pending" and snapshot.confirmation_entry_id is None

    after_ids = []

    async def after(ctx, signal):
        after_ids.append(ctx.tool_call.id)

    for field, value in [
        ("proposal_id", new_id()), ("display_entry_id", new_id()),
        ("confirmation_entry_id", new_id()),
        ("confirmation_entry_id", original_context.source_entry_id),
        ("confirmation_entry_id", snapshot.request_entry_id),
    ]:
        invalid = invocation(kind, {**arguments.model_dump(), field: value})
        failed = await run_tool_call(
            invalid, tools=tools, declared=DECLARED,
            before_tool_call=before, after_tool_call=after,
        )
        assert failed.is_error and "权限检查失败" not in text(failed)
        assert not after_ids
        assert await get_snapshot(arguments.proposal_id) == snapshot
        assert f.database.connection.total_changes == changes

    other = original_context.model_copy(update={"session_id": await f.session("其他会话")})
    other_tools = bind_business_tools(f.business, other, f.call, {}, {}, {})
    denied = await run_tool_call(call, tools=other_tools, declared=DECLARED, before_tool_call=before)
    assert denied.is_error and "access_denied" in text(denied)
    if kind in {"save_workout", "update_workout"}:
        wrong = "update_workout" if kind == "save_workout" else "save_workout"
        denied = await run_tool_call(
            invocation(wrong, arguments.model_dump()), tools=tools, declared=DECLARED,
            before_tool_call=before, after_tool_call=after,
        )
        assert denied.is_error and "类型不一致" in text(denied) and not after_ids
    if kind != "save_profile_update":
        wrong_kind = await run_tool_call(
            invocation("save_profile_update", arguments.model_dump()), tools=tools,
            declared=DECLARED, before_tool_call=before, after_tool_call=after,
        )
        assert wrong_kind.is_error and "profile_proposal_not_found" in text(wrong_kind)
        assert not after_ids

    current = await f.service.get_session(original_context.session_id)
    async with f.repository.transaction():
        await f.database.connection.execute(
            "UPDATE sessions SET active_leaf_id = ? WHERE id = ?",
            (snapshot.request_entry_id, original_context.session_id),
        )
    off_path = await run_tool_call(
        call, tools=tools, declared=DECLARED, before_tool_call=before, after_tool_call=after,
    )
    assert off_path.is_error and not after_ids
    executed = await execute_prepared_tool_call(checked)
    assert executed.message.is_error
    assert await get_snapshot(arguments.proposal_id) == snapshot
    async with f.repository.transaction():
        await f.database.connection.execute(
            "UPDATE sessions SET active_leaf_id = ? WHERE id = ?",
            (current.active_leaf_id, original_context.session_id),
        )

    first = await run_tool_call(
        call, tools=tools, declared=DECLARED, before_tool_call=before, after_tool_call=after,
    )
    assert not first.is_error, text(first)
    saved = json.loads(text(first))
    assert after_ids == [call.id]
    assert (await get_snapshot(arguments.proposal_id)).status == "saved"
    again = await run_tool_call(call, tools=tools, declared=DECLARED, before_tool_call=before)
    assert not again.is_error and json.loads(text(again)) == saved
    rebound = await run_tool_call(
        invocation(kind, {**arguments.model_dump(),
                          "display_entry_id": new_id(), "confirmation_entry_id": new_id()}),
        tools=tools, declared=DECLARED, before_tool_call=before,
    )
    assert not rebound.is_error and json.loads(text(rebound)) == saved
    regenerated = await _regenerate(f, original_context.session_id, snapshot.request_entry_id)
    await f.service.finish_run(original_context.session_id, regenerated.run.id, "completed")
    assert await get_snapshot(arguments.proposal_id) is None
    again = await run_tool_call(call, tools=tools, declared=DECLARED, before_tool_call=before)
    assert not again.is_error and json.loads(text(again)) == saved
    assert (await f.business.list_workouts(WorkoutListArguments())).total <= 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_business_denial_batch_isolation(f, mode):
    context, arguments, get_snapshot = await business_case(f, "save_profile_update")
    snapshot = await get_snapshot(arguments.proposal_id)
    tools = bind_business_tools(f.business, context, f.call, {}, {}, {})
    calls = [
        invocation("save_profile_update", {**arguments.model_dump(), "display_entry_id": new_id()}),
        invocation("get_profile", {}),
        invocation("get_profile_update_status", {"proposal_id": arguments.proposal_id}),
    ]
    # 实际业务实现保持不变；仅切换调度方式覆盖两个 Harness 分支。
    tools = {name: replace(tool, execution_mode=mode) for name, tool in tools.items()}
    after_ids = []

    async def after(ctx, signal):
        after_ids.append(ctx.tool_call.id)

    result = await run_tool_batch(
        calls, tools=tools, declared=DECLARED, execution_mode=mode,
        before_tool_call=create_before_tool_call(f.business, f.call), after_tool_call=after,
    )
    assert result.failure is None
    assert [message.is_error for message in result.messages] == [True, False, False]
    assert set(after_ids) == {calls[1].id, calls[2].id}
    assert await get_snapshot(arguments.proposal_id) == snapshot
    assert (await f.business.get_profile()).version is None

    async with f.repository.transaction():
        await f.database.connection.execute(
            "ALTER TABLE profile_snapshots RENAME TO unavailable_snapshots"
        )
    signal = Event()
    after_ids.clear()
    result = await run_tool_batch(
        calls[:2], tools=tools, declared=DECLARED, execution_mode=mode,
        before_tool_call=create_before_tool_call(f.business, f.call), signal=signal,
        after_tool_call=after,
    )
    assert result.failure is None and not signal.is_set()
    assert after_ids == [calls[1].id]
    assert [message.is_error for message in result.messages] == [True, False]
    assert "权限检查失败" in text(result.messages[0])
    assert "no such table" in text(result.messages[0])
    assert (await f.business.get_profile()).version is None
    protected = await run_tool_batch(
        calls[:2], tools=tools, declared=DECLARED, execution_mode=mode,
        before_tool_call=create_before_tool_call(f.business, f.call),
        contains_credentials=lambda value: "profile_snapshots" in value,
    )
    assert isinstance(protected.failure, CredentialDetectedError)
    assert protected.messages == []


@pytest.mark.anyio
async def test_credentials_and_cancellation(f):
    root = f.path.parent / "credentials"
    session = new_id()
    tools = create_file_tools(session, tmp_root=root)
    prefix = f"sessions/{session}/workspace"
    directory = root / prefix
    directory.mkdir(parents=True)
    secret = "credential-before-permission-check"
    (directory / secret).write_text(secret, encoding="utf-8")
    before = create_before_tool_call(f.business, f.call)
    signal = Event()
    result = await run_tool_batch(
        [invocation("write", {"path": f"{prefix}/{secret}/child", "content": "denied"}),
         invocation("write", {"path": f"{prefix}/later", "content": "denied"})],
        tools=tools, declared=declarations(tools), before_tool_call=before,
        contains_credentials=lambda value: secret in value, signal=signal,
    )
    assert isinstance(result.failure, CredentialDetectedError)
    assert result.messages == [] and signal.is_set()
    assert not (directory / "later").exists()
    with pytest.raises(CredentialDetectedError):
        await run_tool_call(
            invocation("read", {"path": f"{prefix}/{secret}"}), tools=tools,
            declared=declarations(tools), before_tool_call=before,
            contains_credentials=lambda value: secret in value,
        )
    cancelled = await run_tool_batch(
        [invocation("write", {"path": f"{prefix}/cancelled", "content": "denied"})],
        tools=tools, declared=declarations(tools), before_tool_call=before, signal=signal,
    )
    assert cancelled.messages == [] and not (directory / "cancelled").exists()

    context, arguments, _ = await business_case(f, "save_profile_update")
    tools = bind_business_tools(f.business, context, f.call, {}, {}, {})
    signal = Event()
    async with f.repository.transaction():
        task = asyncio.create_task(run_tool_batch(
            [invocation("save_profile_update", arguments.model_dump())],
            tools=tools, declared=DECLARED, before_tool_call=before, signal=signal,
        ))
        await asyncio.sleep(0.05)
        task.cancel()
        result = await task
        assert isinstance(result.failure, asyncio.CancelledError)
        assert signal.is_set() and result.messages == []
    # 等待只读检查线程完成，确保测试释放真实 SQLite 连接前无在途读取。
    await asyncio.get_running_loop().shutdown_default_executor()
    assert (await f.snapshot(arguments.proposal_id)).status == "pending"
