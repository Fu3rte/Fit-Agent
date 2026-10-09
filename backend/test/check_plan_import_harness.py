import asyncio
import copy
import json
import traceback
from dataclasses import replace
from threading import Event

import pytest

from app.agent.tool import (
    AfterToolCallResult,
    BeforeToolCallResult,
    CredentialDetectedError,
    prepare_arguments,
    run_tool_batch,
    run_tool_call,
    validate_arguments,
)
from app.ai.messages import TextContent, text_projection
from app.interfaces.http import CredentialFilter
from test.check_plan_import_tools import (
    DECLARED,
    INCOMPLETE,
    ROOT,
    binding,
    body,
    invocation,
)
from test.check_profile_confirmation import Fixture, count_rows

MARKER = "PLAN-HARNESS-CREDENTIAL-314159"
GUARD = CredentialFilter((MARKER,)).contains


async def check_boundaries(selected):
    f = await Fixture(ROOT / f"{selected}.db").seeded()
    try:
        session = await f.session("Harness安全与收尾")
        args = {"base_profile_version": None, "base_plan_id": None, "payload": copy.deepcopy(INCOMPLETE)}
        call = invocation("prepare_plan_import", args)
        run, _, tools, registrations = await binding(f, session, [call])
        tool = tools[call.name]
        if selected == "publication":
            await check_publication_chains(f, tool, args, registrations)
        elif selected == "cancellation":
            await check_repeated_cancel(f, tool, args, registrations)
        else:
            raise ValueError("未知专项")
        await f.service.finish_run(session, run.id, "completed")
        print(f"PASS: {selected} boundary; real SQLite preparation and controlled lifecycle")
        print("Evidence:", ROOT)
    finally:
        await f.close()


async def check_publication_chains(f, tool, args, registrations):
    after_calls, completed, attempts = [], [], []

    async def after(context, signal):
        after_calls.append(context.tool_call.id)

    async def finalized(outcome):
        completed.append(outcome.message)

    def progress(tool_call_id, params, signal, on_update):
        result = tool.execute(tool_call_id, params, signal, None)
        on_update(result)
        return result

    def publication(result, *, explicit, cyclic, sensitive=True):
        attempts.append(result)
        try:
            directory = MARKER if sensitive else "missing-publication-directory"
            (ROOT / directory / "missing-publication.json").write_text(
                text_projection(result.content), encoding="utf-8")
        except OSError as error:
            wrapped = RuntimeError("进度存储失败")
            if cyclic:
                error.__cause__ = wrapped
            if explicit:
                raise wrapped from error
            raise wrapped

    changed = replace(tool, execute=progress)
    for index, (explicit, cyclic) in enumerate(((True, False), (False, False), (True, True))):
        call = invocation(tool.name, args)
        before_rows = await count_rows(f.database, "plan_snapshots")
        failure = None
        try:
            await run_tool_call(call, tools={tool.name: changed}, declared=DECLARED,
                on_update=lambda result: publication(result, explicit=explicit, cyclic=cyclic),
                after_tool_call=after, contains_credentials=GUARD)
        except BaseException as error:
            failure = error
        assert isinstance(failure, CredentialDetectedError), "发布异常链须触发安全中止"
        assert MARKER not in "".join(traceback.format_exception(failure))
        assert not after_calls and not completed
        assert await count_rows(f.database, "plan_snapshots") == before_rows + 1
        assert call.id in registrations
        assert len(attempts) == 2 * index + 1
        call, following = invocation(tool.name, args), invocation(tool.name, args)
        before_rows = await count_rows(f.database, "plan_snapshots")
        outcome = await run_tool_batch([call, following], tools={tool.name: changed}, declared=DECLARED,
            on_tool_update=lambda call_id, name, result: publication(result, explicit=explicit, cyclic=cyclic),
            on_tool_finalized=finalized, after_tool_call=after, contains_credentials=GUARD)
        assert isinstance(outcome.failure, CredentialDetectedError), "批次发布异常链须触发安全中止"
        assert MARKER not in "".join(traceback.format_exception(outcome.failure))
        assert not outcome.messages and not completed and not after_calls
        assert await count_rows(f.database, "plan_snapshots") == before_rows + 1
        assert call.id in registrations and following.id not in registrations
    assert len(attempts) == 6
    for explicit in (True, False):
        call = invocation(tool.name, args)
        failure = None
        try:
            await run_tool_call(call, tools={tool.name: changed}, declared=DECLARED,
                on_update=lambda result: publication(result, explicit=explicit, cyclic=False, sensitive=False),
                after_tool_call=after, contains_credentials=GUARD)
        except BaseException as error:
            failure = error
        assert isinstance(failure, RuntimeError)
        diagnostic = "".join(traceback.format_exception(failure))
        assert "进度存储失败" in diagnostic and "FileNotFoundError" in diagnostic
        assert "missing-publication-directory" in diagnostic and MARKER not in diagnostic
        assert not after_calls and not completed


async def check_repeated_cancel(f, tool, args, registrations):
    for batch in (False, True):
        ready, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        signal = Event()
        after_calls, execution_calls, completed = [], [], []
        call, following = invocation(tool.name, args), invocation(tool.name, args)
        before_rows = await count_rows(f.database, "plan_snapshots")

        async def waiting(tool_call_id, params, signal, on_update):
            execution_calls.append(tool_call_id)
            result = await asyncio.to_thread(tool.execute, tool_call_id, params, signal, on_update)
            ready.set()
            await release.wait()
            finished.set()
            return result

        async def after(context, signal):
            assert finished.is_set(), "after必须等待真实执行终态"
            after_calls.append(context)

        async def finalized(outcome):
            completed.append(outcome.message)

        async def turn():
            checkpoint = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
            await checkpoint

        changed = replace(tool, execute=waiting)
        if batch:
            task = asyncio.create_task(run_tool_batch([call, following], tools={tool.name: changed},
                declared=DECLARED, signal=signal, after_tool_call=after,
                on_tool_finalized=finalized, contains_credentials=GUARD))
        else:
            task = asyncio.create_task(run_tool_call(call, tools={tool.name: changed},
                declared=DECLARED, signal=signal, after_tool_call=after, contains_credentials=GUARD))
        try:
            await ready.wait()
            task.cancel()
            await asyncio.to_thread(signal.wait)
            await turn()
            task.cancel()
            await turn()
            assert not after_calls and not task.done() and not finished.is_set(), "重复取消必须持续等待真实收尾"
        finally:
            release.set()
            failure, outcome = None, None
            try:
                outcome = await task
            except BaseException as error:
                failure = error
        if batch:
            assert failure is None and isinstance(outcome.failure, asyncio.CancelledError)
            assert not outcome.messages
        else:
            assert isinstance(failure, asyncio.CancelledError)
        assert finished.is_set() and signal.is_set()
        assert len(after_calls) == 1 and after_calls[0].is_error
        assert json.loads(after_calls[0].result.content[0].text)["proposal_id"] == registrations[call.id]
        assert execution_calls == [call.id] and following.id not in registrations and not completed
        assert await count_rows(f.database, "plan_snapshots") == before_rows + 1
        snapshot = await f.repository.get_plan_snapshot(registrations[call.id])
        assert snapshot.payload.model_dump() == args["payload"]


async def check():
    await check_boundaries("publication")
    await check_boundaries("cancellation")
    f = await Fixture(ROOT / "harness.db").seeded()
    try:
        session = await f.session("工具生命周期")
        args = {"base_profile_version": None, "base_plan_id": None, "payload": copy.deepcopy(INCOMPLETE)}
        calls = [invocation("prepare_plan_import", args), invocation("prepare_plan_adjustment", args)]
        run, _, tools, registrations = await binding(f, session, calls)
        name = calls[0].name
        tool = tools[name]
        baseline = [call.model_dump() for call in calls]
        assert prepare_arguments(tool, calls[0]) == calls[0].arguments
        assert validate_arguments(tool, calls[0].arguments).model_dump() == calls[0].arguments
        events = []

        async def before(context, signal):
            assert context.arguments.model_dump() == args
            assert context.trusted_context.session_id == session
            assert signal is None
            events.append(("before", context.tool_call.id))
            context.arguments.payload.days[0].focus = "污染"
            context.tool_call.arguments["payload"]["notes"] = "污染"
            object.__setattr__(context.trusted_context, "session_id", "污染")

        async def after(context, signal):
            assert context.duration_ms >= 0 and not context.is_error
            events.append(("after", context.tool_call.id))
            data = json.loads(context.result.content[0].text)
            assert data["payload"] == args["payload"]
            context.result.content[0].text = "污染"
            context.arguments.payload.days[0].focus = "污染"

        outcomes = await run_tool_batch(calls, tools=tools, declared=DECLARED,
            before_tool_call=before, after_tool_call=after, contains_credentials=GUARD)
        assert outcomes.failure is None
        assert events == [("before", calls[0].id), ("after", calls[0].id), ("before", calls[1].id), ("after", calls[1].id)]
        assert [call.model_dump() for call in calls] == baseline
        assert [message.tool_call_id for message in outcomes.messages] == [call.id for call in calls]
        assert all(body(message)["payload"] == args["payload"] for message in outcomes.messages)
        assert tools[name].trusted_context.session_id == session
        assert set(registrations) == {call.id for call in calls}
        before_rows = await count_rows(f.database, "plan_snapshots")
        after_events = []

        async def observed_after(context, signal):
            after_events.append(context)

        async def block(context, signal):
            assert context.trusted_context.session_id == session
            return BeforeToolCallResult(block=True, reason="权限策略拒绝")

        async def permission_error(context, signal):
            (ROOT / "missing-permission-resource").read_text(encoding="utf-8")

        def prepare_error(arguments):
            (ROOT / "missing-prepare-resource").read_text(encoding="utf-8")

        for changed, pre, arguments, marker in (
            (tool, block, args, "权限策略拒绝"),
            (tool, permission_error, args, "权限检查失败"),
            (replace(tool, prepare_arguments=prepare_error), None, args, "参数预处理失败"),
            (tool, None, {**args, "base_profile_version": "null"}, "base_profile_version"),
        ):
            message = await run_tool_call(invocation(name, arguments), tools={name: changed},
                declared=DECLARED, before_tool_call=pre, after_tool_call=observed_after)
            assert message.is_error and marker in text_projection(message.content)
        signal = Event()
        signal.set()
        message = await run_tool_call(invocation(name, args), tools=tools, declared=DECLARED,
            signal=signal, after_tool_call=observed_after)
        assert message.is_error and "aborted" in text_projection(message.content)
        assert not after_events and await count_rows(f.database, "plan_snapshots") == before_rows

        def execute_then_read(tool_call_id, params, signal, on_update):
            result = tool.execute(tool_call_id, params, signal, on_update)
            assert not result.is_error
            (ROOT / "missing-execute-resource").read_text(encoding="utf-8")
            return result

        changed = replace(tool, execute=execute_then_read)
        call = invocation(name, args)
        message = await run_tool_call(call, tools={name: changed}, declared=DECLARED,
            after_tool_call=observed_after)
        assert message.is_error and "工具执行失败" in text_projection(message.content)
        assert len(after_events) == 1 and after_events[-1].is_error
        assert call.id in registrations and await count_rows(f.database, "plan_snapshots") == before_rows + 1

        async def after_error(context, signal):
            assert not context.is_error
            (ROOT / "missing-after-resource").read_text(encoding="utf-8")

        call = invocation(name, args)
        message = await run_tool_call(call, tools=tools, declared=DECLARED, after_tool_call=after_error)
        assert message.is_error and len(message.content) == 2
        actual = json.loads(message.content[0].text)
        assert registrations[call.id] == actual["proposal_id"]
        assert (await f.repository.get_plan_snapshot(actual["proposal_id"])).payload.model_dump() == args["payload"]
        assert "副作用" in message.content[1].text

        retained = []

        async def preserving_after(context, signal):
            retained.extend(context.result.content)
            return AfterToolCallResult(content=retained, is_error=context.is_error)

        message = await run_tool_call(invocation(name, args), tools=tools, declared=DECLARED,
            after_tool_call=preserving_after)
        original_text = message.content[0].text
        retained[0].text = "污染"
        assert message.content[0].text == original_text and not message.is_error

        started = asyncio.Event()
        finished = []
        signal = Event()

        async def waiting(tool_call_id, params, signal, on_update):
            result = await asyncio.to_thread(tool.execute, tool_call_id, params, signal, on_update)
            started.set()
            while not signal.is_set():
                await asyncio.sleep(0.001)
            finished.append(tool_call_id)
            return result

        cancel_tool = replace(tool, execute=waiting)
        call = invocation(name, args)
        task = asyncio.create_task(run_tool_call(call, tools={name: cancel_tool}, declared=DECLARED,
            signal=signal, after_tool_call=observed_after, contains_credentials=GUARD))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert signal.is_set() and finished == [call.id]
        assert after_events[-1].tool_call.id == call.id and after_events[-1].is_error
        assert registrations[call.id] == json.loads(after_events[-1].result.content[0].text)["proposal_id"]

        async def inner_cancel(tool_call_id, params, signal, on_update):
            await asyncio.to_thread(tool.execute, tool_call_id, params, signal, on_update)
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

        call = invocation(name, args)
        with pytest.raises(asyncio.CancelledError):
            await run_tool_call(call, tools={name: replace(tool, execute=inner_cancel)}, declared=DECLARED,
                signal=Event(), after_tool_call=observed_after)
        assert after_events[-1].tool_call.id == call.id and after_events[-1].is_error
        assert call.id in registrations

        async def leak_before(context, signal):
            return BeforeToolCallResult(block=True, reason=MARKER)

        async def leak_after(context, signal):
            return AfterToolCallResult(content=[TextContent(type="text", text=MARKER)])

        def leak_prepare(arguments):
            raise ValueError(MARKER)

        def progress(tool_call_id, params, signal, on_update):
            result = tool.execute(tool_call_id, params, signal, None)
            on_update(result)
            return result

        leaking_args = copy.deepcopy(args)
        leaking_args["payload"]["notes"] = "x" * 60000 + MARKER
        for changed, arguments, pre, post in (
            (tool, leaking_args, None, None),
            (tool, {**leaking_args, "base_profile_version": "null"}, None, None),
            (tool, args, leak_before, None),
            (replace(tool, prepare_arguments=leak_prepare), args, None, None),
            (tool, args, None, leak_after),
            (replace(tool, execute=progress), leaking_args, None, None),
        ):
            with pytest.raises(CredentialDetectedError):
                await run_tool_call(invocation(name, arguments), tools={name: changed}, declared=DECLARED,
                    before_tool_call=pre, after_tool_call=post, contains_credentials=GUARD)
            outcome = await run_tool_batch([invocation(name, arguments)], tools={name: changed}, declared=DECLARED,
                before_tool_call=pre, after_tool_call=post, contains_credentials=GUARD)
            assert isinstance(outcome.failure, CredentialDetectedError) and not outcome.messages

        async def safety_before(context, signal):
            raise CredentialDetectedError()

        def safety_prepare(arguments):
            raise CredentialDetectedError()

        rows = await count_rows(f.database, "plan_snapshots")
        for changed, pre in ((tool, safety_before), (replace(tool, prepare_arguments=safety_prepare), None)):
            with pytest.raises(CredentialDetectedError):
                await run_tool_call(invocation(name, args), tools={name: changed}, declared=DECLARED,
                    before_tool_call=pre, after_tool_call=observed_after)
        assert await count_rows(f.database, "plan_snapshots") == rows

        safety_after = []
        safety_calls = []
        safety_publications = []

        async def safety_after_hook(context, signal):
            safety_after.append(context.tool_call.id)

        def execute_safety(tool_call_id, params, signal, on_update):
            safety_calls.append(tool_call_id)
            result = tool.execute(tool_call_id, params, signal, None)
            if GUARD(text_projection(result.content)):
                raise CredentialDetectedError()
            return result

        async def safety_finalized(outcome):
            safety_publications.append(outcome.message)

        for changed in (tool, replace(tool, execute=execute_safety), replace(tool, execute=progress)):
            call = invocation(name, leaking_args)
            rows = await count_rows(f.database, "plan_snapshots")
            following = invocation(name, args)
            outcome = await run_tool_batch([call, following], tools={name: changed}, declared=DECLARED,
                after_tool_call=safety_after_hook, contains_credentials=GUARD,
                on_tool_finalized=safety_finalized,
                on_tool_update=lambda *update: safety_publications.append(update))
            assert isinstance(outcome.failure, CredentialDetectedError)
            assert not outcome.messages and not safety_after and not safety_publications
            assert await count_rows(f.database, "plan_snapshots") == rows + 1
            assert call.id in registrations and following.id not in registrations
            if changed.execute is execute_safety:
                assert safety_calls.count(call.id) == 1
        assert await count_rows(f.database, "plans") == 0

        publication_after = []
        publication_attempts = []

        async def publication_after_hook(context, signal):
            publication_after.append(context.tool_call.id)

        def publish_failure(result):
            publication_attempts.append(result)
            (ROOT / "missing-publish-parent/progress.json").write_text(
                text_projection(result.content), encoding="utf-8")

        progress_tool = replace(tool, execute=progress)
        call = invocation(name, args)
        rows = await count_rows(f.database, "plan_snapshots")
        with pytest.raises(RuntimeError, match="进度发布失败") as failure:
            await run_tool_call(call, tools={name: progress_tool}, declared=DECLARED,
                after_tool_call=publication_after_hook, on_update=publish_failure,
                contains_credentials=GUARD)
        assert isinstance(failure.value.__cause__, FileNotFoundError)
        assert len(publication_attempts) == 1 and not publication_after
        assert await count_rows(f.database, "plan_snapshots") == rows + 1 and call.id in registrations
        call, following = invocation(name, args), invocation(name, args)
        rows = await count_rows(f.database, "plan_snapshots")
        completed = []

        async def publication_finalized(outcome):
            completed.append(outcome.message)

        outcome = await run_tool_batch([call, following], tools={name: progress_tool}, declared=DECLARED,
            after_tool_call=publication_after_hook,
            on_tool_update=lambda call_id, tool_name, result: publish_failure(result),
            on_tool_finalized=publication_finalized, contains_credentials=GUARD)
        assert isinstance(outcome.failure, RuntimeError)
        assert isinstance(outcome.failure.__cause__, FileNotFoundError)
        assert not outcome.messages and not completed and not publication_after
        assert len(publication_attempts) == 2
        assert await count_rows(f.database, "plan_snapshots") == rows + 1
        assert call.id in registrations and following.id not in registrations

        def sensitive_publish_failure(result):
            (ROOT / MARKER / "progress.json").write_text(text_projection(result.content), encoding="utf-8")

        with pytest.raises(CredentialDetectedError):
            await run_tool_call(invocation(name, args), tools={name: progress_tool}, declared=DECLARED,
                on_update=sensitive_publish_failure, contains_credentials=GUARD)

        def invalid_result(tool_call_id, params, signal, on_update):
            tool.execute(tool_call_id, params, signal, on_update)
            return {"content": []}

        invalid = replace(tool, execute=invalid_result)
        with pytest.raises(TypeError, match="协议"):
            await run_tool_call(invocation(name, args), tools={name: invalid}, declared=DECLARED)
        duplicate = invocation(name, args)
        with pytest.raises(ValueError, match="重复"):
            await run_tool_batch([duplicate, duplicate], tools=tools, declared=DECLARED)
        with pytest.raises(ValueError, match="不一致"):
            await run_tool_call(invocation(name, args), tools={name: replace(tool, description="changed")}, declared=DECLARED)
        ordered_calls = [invocation(name, args), invocation("prepare_plan_adjustment", args)]
        prepare_order, finish_order = [], []
        second_done = asyncio.Event()

        async def ordered_before(context, signal):
            prepare_order.append(context.tool_call.id)

        async def first_execute(tool_call_id, params, signal, on_update):
            await second_done.wait()
            return await asyncio.to_thread(tool.execute, tool_call_id, params, signal, on_update)

        adjustment = tools["prepare_plan_adjustment"]

        async def second_execute(tool_call_id, params, signal, on_update):
            return await asyncio.to_thread(adjustment.execute, tool_call_id, params, signal, on_update)

        async def finalized(outcome):
            finish_order.append(outcome.message.tool_call_id)
            if outcome.message.tool_call_id == ordered_calls[1].id:
                second_done.set()

        parallel = {
            name: replace(tool, execute=first_execute, execution_mode="parallel"),
            adjustment.name: replace(adjustment, execute=second_execute, execution_mode="parallel"),
        }
        outcome = await run_tool_batch(ordered_calls, tools=parallel, declared=DECLARED,
            before_tool_call=ordered_before, on_tool_finalized=finalized)
        assert outcome.failure is None
        assert prepare_order == [call.id for call in ordered_calls]
        assert finish_order == [ordered_calls[1].id, ordered_calls[0].id]
        assert [message.tool_call_id for message in outcome.messages] == prepare_order
        assert all(not message.is_error for message in outcome.messages)

        nested_call = invocation(adjustment.name, args)
        nested_events = []

        async def nested_before(context, signal):
            nested_events.append(context.tool_call.name)

        async def nested_execute(tool_call_id, params, signal, on_update):
            nested_result = await run_tool_call(nested_call, tools=tools, declared=DECLARED,
                before_tool_call=nested_before, signal=signal, contains_credentials=GUARD)
            assert not nested_result.is_error and nested_call.id in registrations
            return await asyncio.to_thread(tool.execute, tool_call_id, params, signal, on_update)

        message = await run_tool_call(invocation(name, args),
            tools={name: replace(tool, execute=nested_execute)}, declared=DECLARED,
            before_tool_call=nested_before, contains_credentials=GUARD)
        assert not message.is_error and nested_events == [name, adjustment.name]
        await f.service.finish_run(session, run.id, "completed")
        print("PASS: real prepare/validate/before/execute/after; frozen context and argument isolation; denial/preparation/schema failures; actual execute and after exceptions; cancellation after committed preparation; full credentials; protocol invariants")
        print("Evidence:", ROOT)
    finally:
        await f.close()


if __name__ == "__main__":
    asyncio.run(check())
