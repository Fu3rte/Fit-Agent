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
from test.check_plan_core import profile
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
        assistants, historical = [], []
        for result_id, _, parent_id, raw_result, raw_assistant in samples:
            assert result_id in path_ids and parent_id in path_ids
            result = json.loads(raw_result)[0]
            assistant = AssistantMessage.model_validate(json.loads(raw_assistant)[0])
            calls = [block for block in assistant.content if isinstance(block, ToolCall)]
            assert len(calls) == 1 and calls[0].id == result["tool_call_id"] and calls[0].name == result["tool_name"]
            # 历史证据保真：源库存储的失败结果与非法参数原文保持逐字不变。
            assert calls[0].arguments["base_plan_id"] == "None"
            assert calls[0].arguments["base_profile_version"] == 1
            assert 'base_plan_id' in result["content"][0]["text"] and 'String should match pattern' in result["content"][0]["text"]
            assistants.append(assistant)
            historical.append(result["content"][0]["text"])
        print("Historical source result/assistant IDs:", [(row[0], row[2]) for row in samples])

    fixture = await Fixture(temporary_root("plan-integration-guard") / f"{uuid4().hex}.db").seeded()
    try:
        integration.STRICT_FAILURES.clear()
        integration.TOOL_COUNTS.update(prepare_plan=0, save_plan=0)
        integration.PREPARE_LIMIT = 8
        session = await fixture.session("真实生成链新契约")
        # 新契约：真实模型消息里的空依据字符串经参数预处理归一为 null，需要已保存画像作为生成依据。
        await profile(fixture, session)
        assert (await fixture.business.get_profile()).version == 1
        declared = {tool.name: tool for tool in business_tool_declarations()}
        registered: dict[str, str] = {}
        proposals = []
        for assistant, history_text in zip(assistants, historical):
            outcome = await fixture.service.accept_send(SendCommand(operation_id=str(uuid4()), session_id=session,
                request=SendRequest(text="依据已保存画像生成完整计划建议")), system_message=SYSTEM)
            request = outcome.run.request_entry_id
            source_id = await fixture.append(session, outcome.run.id, request, assistant)
            tools = bind_business_tools(fixture.business, fixture.context(session, request, source_id),
                                        fixture.call, {}, {}, registered)
            calls = [block for block in assistant.content if isinstance(block, ToolCall)]

            async def finalized(item):
                integration.observe_event({"type": "tool_execution_end", "name": item.message.tool_name,
                    "is_error": item.message.is_error, "content": text_projection(item.message.content)})

            batch = await run_tool_batch(calls, tools=tools, declared=declared, on_tool_finalized=finalized)
            assert batch.failure is None and len(batch.messages) == 1
            result = batch.messages[0]
            assert not result.is_error, text_projection(result.content)
            detail = json.loads(text_projection(result.content))
            assert detail["base_plan_id"] is None and detail["base_profile_version"] == 1
            assert calls[0].id in registered and registered[calls[0].id] == detail["proposal_id"]
            snapshot = await fixture.repository.get_plan_snapshot(detail["proposal_id"])
            assert snapshot.preparation_kind == "generation" and snapshot.base_plan_id is None
            assert snapshot.payload.model_dump() == detail["payload"]
            result_id = await fixture.append(session, outcome.run.id, source_id, result)
            persisted = await fixture.service.get_entry(session, result_id)
            assert persisted.messages[0] == result
            await fixture.service.finish_run(session, outcome.run.id, "completed")
            proposals.append(detail["proposal_id"])
        # 后一次真实生成使前一次 pending 失效，历史快照保留。
        assert len(proposals) == 2 and proposals[0] != proposals[1]
        assert (await fixture.repository.get_plan_snapshot(proposals[0])).status == "invalidated"
        assert (await fixture.repository.get_plan_snapshot(proposals[1])).status == "pending"
        current = await fixture.business.get_current_plan()
        assert current.id is None and current.content is None

        # 守卫直测：连续两次相同真实严格失败文本必须停止下一模型请求，守卫实现保持不变。
        integration.STRICT_FAILURES.clear()
        integration.STRICT_FAILURES.extend([historical[0], historical[0]])
        try:
            integration.check_request_budget()
        except RuntimeError as error:
            assert "连续相同计划参数失败" in str(error), str(error)
        else:
            raise AssertionError("连续相同计划参数失败未触发请求预算守卫")
        integration.STRICT_FAILURES.clear()
        assert not integration.REQUESTS
        branch = await fixture.service.get_current_branch(session)
        assert not branch[-1].messages[0].is_error
        assert not await fixture.business.list_plans()
        await fixture.restart()
        assert not (await fixture.service.get_current_branch(session))[-1].messages[0].is_error
        print("PASS: real persisted model arguments, harness empty-basis normalization to JSON null, "
              "generation snapshots, next-request guard direct trip, restart; zero model requests")
        print("Historical strict failure text sample:", history_text[:60], "...")
        print("Guard SQLite:", fixture.path, "Observation evidence:", integration.ROOT)
    finally:
        await fixture.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-database", required=True)
    asyncio.run(check(parser.parse_args().source_database))
