import argparse
import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.agent.tool import run_tool_batch
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.ai.messages import AssistantMessage, ToolCall, text_projection
from app.domain.session.models import SendCommand, SendRequest
from test import check_plan_integration as integration
from test.check_profile_confirmation import SYSTEM, Fixture
from test.regression_support import temporary_root


async def check(source):
    with sqlite3.connect(f"file:{Path(source).resolve().as_posix()}?mode=ro", uri=True) as db:
        samples = db.execute("""SELECT t.id,t.session_id,t.parent_id,t.messages,a.messages
            FROM session_entries t JOIN session_entries a
            ON a.id=t.parent_id AND a.session_id=t.session_id
            WHERE json_extract(t.messages,'$[0].role')='toolResult'
            AND json_extract(t.messages,'$[0].tool_name')='prepare_plan'
            AND json_extract(t.messages,'$[0].is_error')=1
            ORDER BY t.created_at DESC,t.id DESC LIMIT 2""").fetchall()[::-1]
        assert len(samples) == 2 and samples[0][1] == samples[1][1]
        path_ids = {row[0] for row in db.execute("""WITH RECURSIVE branch(id,parent_id) AS (
            SELECT e.id,e.parent_id FROM session_entries e JOIN sessions s
            ON s.active_leaf_id=e.id AND s.id=e.session_id WHERE s.id=?
            UNION ALL SELECT e.id,e.parent_id FROM session_entries e JOIN branch b
            ON e.id=b.parent_id WHERE e.session_id=?) SELECT id FROM branch""", (samples[0][1], samples[0][1]))}
        assistants = []
        for result_id, _, parent_id, raw_result, raw_assistant in samples:
            assert result_id in path_ids and parent_id in path_ids
            result = json.loads(raw_result)[0]
            assistant = AssistantMessage.model_validate(json.loads(raw_assistant)[0])
            calls = [block for block in assistant.content if isinstance(block, ToolCall)]
            assert len(calls) == 1 and calls[0].id == result["tool_call_id"] and calls[0].name == result["tool_name"]
            assert calls[0].arguments["base_plan_id"] == "None"
            assert 'base_plan_id' in result["content"][0]["text"] and 'String should match pattern' in result["content"][0]["text"]
            assistants.append(assistant)
        print("Real source result/assistant IDs:", [(row[0], row[2]) for row in samples])
    fixture = await Fixture(temporary_root("plan-integration-guard") / f"{uuid4().hex}.db").seeded()
    try:
        integration.STRICT_FAILURES.clear()
        integration.TOOL_COUNTS.update(prepare_plan=0, save_plan=0)
        integration.PREPARE_LIMIT = 8
        session = await fixture.session("真实错误结果持久化守卫")
        for assistant in assistants:
            outcome = await fixture.service.accept_send(SendCommand(operation_id=str(uuid4()), session_id=session,
                request=SendRequest(text="核对真实参数错误")), system_message=SYSTEM)
            request = outcome.run.request_entry_id
            source_id = await fixture.append(session, outcome.run.id, request, assistant)
            tools = bind_business_tools(fixture.business, fixture.context(session, request, source_id), fixture.call, {}, {}, {})
            calls = [block for block in assistant.content if isinstance(block, ToolCall)]

            async def finalized(item):
                integration.observe_event({"type": "tool_execution_end", "name": item.message.tool_name,
                    "is_error": item.message.is_error, "content": text_projection(item.message.content)})

            batch = await run_tool_batch(calls, tools=tools,
                declared={tool.name: tool for tool in business_tool_declarations()}, on_tool_finalized=finalized)
            assert batch.failure is None and len(batch.messages) == 1
            result = batch.messages[0]
            assert result.is_error and 'base_plan_id' in text_projection(result.content)
            schema = json.dumps({"base_plan_id": tools["prepare_plan"].definition().parameters["properties"]["base_plan_id"]}, ensure_ascii=False, indent=2)
            assert schema in text_projection(result.content)
            assert '"type": "null"' in text_projection(result.content)
            result_id = await fixture.append(session, outcome.run.id, source_id, result)
            persisted = await fixture.service.get_entry(session, result_id)
            assert persisted.messages[0] == result
            await fixture.service.finish_run(session, outcome.run.id, "completed")
        arguments = dict(calls[0].arguments, base_plan_id=None)
        accepted_null = await fixture.invoke("prepare_plan", arguments, fixture.context(session, request, source_id))
        assert json.loads(text_projection(accepted_null.content))["code"] == "profile_required"
        unknown = await fixture.invoke("prepare_plan", dict(arguments, unexpected=1), fixture.context(session, request, source_id))
        assert unknown.is_error and 'unexpected' in text_projection(unknown.content)
        branch = await fixture.service.get_current_branch(session)
        assert branch[-1].messages[0].role == "toolResult"
        try:
            integration.check_request_budget()
        except RuntimeError as error:
            assert "连续相同计划参数失败" in str(error)
        else:
            raise AssertionError("连续参数失败未停止下一模型请求")
        assert not integration.REQUESTS
        await fixture.restart()
        assert (await fixture.service.get_current_branch(session))[-1].messages[0].is_error
        print("PASS: real persisted model arguments, harness strict errors, complete SQLite tool pairing, next-request guard, restart; zero model requests")
        print("Guard SQLite:", fixture.path, "Observation evidence:", integration.ROOT)
    finally:
        await fixture.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-database", required=True)
    asyncio.run(check(parser.parse_args().source_database))
