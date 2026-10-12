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
from test.check_profile_confirmation import DECLARED, Fixture, new_id
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
    before = create_before_tool_call()
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
    before = create_before_tool_call()
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


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_business_save_tools_removed(f, mode):
    # 模型声明与执行注册表均不提供四项保存工具；请求按“工具不存在”拒绝且零业务改动。
    session = await f.session("权限保存边界")
    context = f.context(session, new_id(), new_id())
    tools = bind_business_tools(f.business, context, f.call, {}, {}, {})
    assert declarations(tools) == DECLARED
    removed = ("save_profile_update", "save_plan", "save_workout", "update_workout")
    assert not (set(removed) & set(tools))
    tools = {name: replace(tool, execution_mode=mode) for name, tool in tools.items()}
    before = create_before_tool_call()
    after_ids = []

    async def after(ctx, signal):
        after_ids.append(ctx.tool_call.id)

    changes = f.database.connection.total_changes
    for name in removed:
        message = await run_tool_call(
            invocation(name, {
                "proposal_id": new_id(), "display_entry_id": new_id(),
                "confirmation_entry_id": new_id(),
            }),
            tools=tools, declared=DECLARED, before_tool_call=before, after_tool_call=after,
        )
        assert message.is_error and "工具不存在" in text(message), name
    assert after_ids == []
    assert f.database.connection.total_changes == changes


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
    before = create_before_tool_call()
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
