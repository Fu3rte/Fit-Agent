import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tool import AgentTool
from app.agent.tools.business import (
    bind_business_tools,
    business_tool_declarations,
)
from app.ai.context import get_current_system_prompt
from app.ai.messages import (
    AssistantMessage,
    JsonObject,
    Model,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from app.ai.stream import AssistantResponse
from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.service import BusinessService, business_date
from app.application.session.service import SessionService
from app.domain.business.models import BusinessContext
from app.domain.session.models import SendCommand, SendRequest, SessionMessageEntry
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.model_config import load_model_config
from test.regression_support import temporary_root

FULL_PROFILE: JsonObject = {
    "goal": "增肌",
    "experience": None,
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}


def usage() -> Usage:
    return Usage(
        input=1, output=1, cache_read=0, cache_write=0, total_tokens=2
    )


def assistant(content, stop_reason: str) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="openai",
        model="test",
        usage=usage(),
        stop_reason=stop_reason,
        timestamp=time_ns() // 1_000_000,
    )


def scripted_stream(turns: list[AssistantMessage], captured: list):
    pending = list(turns)

    def stream_fn(model, context, options):
        captured.append(context["messages"])

        async def source():
            if not pending:
                raise AssertionError("脚本化模型流已用尽")
            message = pending.pop(0)
            partial = message.model_copy(
                update={"content": [], "stop_reason": "pending", "usage": None}
            )
            yield {"type": "start", "partial": partial}
            yield {"type": "done", "reason": message.stop_reason, "message": message}

        return AssistantResponse(source())

    return stream_fn


async def check_loop(directory: Path) -> dict:
    database = await open_database(directory / "loop.db")
    try:
        service = SessionService(SqliteSessionRepository(database))
        repository = SqliteBusinessRepository(database)
        catalog = Catalog.load()
        replacements = ReplacementCoordinator()
        business = BusinessService(repository, catalog, service, replacements)
        service.attach_snapshots(business)
        async with repository.transaction():
            await repository.replace_exercises(catalog.all())

        session_id = str(uuid4())
        await service.create_session(session_id, "loop")
        system_message = SystemMessage(
            role="system",
            content=SYSTEM_PROMPT,
            tools_added=business_tool_declarations(),
            timestamp=time_ns() // 1_000_000,
        )
        outcome = await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id=session_id,
                request=SendRequest(text="我的目标是增肌，环境是健身房，每周练四次。"),
            ),
            system_message=system_message,
        )
        run = outcome.run
        branch = await service.get_context(session_id, run.request_entry_id)
        request_entry = await service.get_entry(session_id, run.request_entry_id)

        loop = asyncio.get_running_loop()
        saved: list[str] = []
        position = {"id": run.request_entry_id}
        # 准备工具按 tool_call_id 记录快照标识，结果节点提交后绑定展示消息。
        prepared: dict[str, str] = {}

        def call(coro):
            return asyncio.run_coroutine_threadsafe(coro, loop).result()

        async def save_message(node_id: str, message: Model) -> None:
            saved.append(node_id)
            await service.append_entry(
                SessionMessageEntry(
                    session_id=session_id,
                    id=node_id,
                    parent_id=position["id"],
                    run_id=run.id,
                    type="message",
                    messages=[message],
                    created_at=time_ns() // 1_000_000,
                )
            )
            position["id"] = node_id
            if (
                isinstance(message, ToolResultMessage)
                and message.tool_name == "prepare_profile_update"
                and not message.is_error
            ):
                proposal_id = prepared.get(message.tool_call_id)
                if proposal_id is not None:
                    await business.bind_display_entry(proposal_id, node_id)

        async def message_nodes(leaf_id: str) -> list[dict]:
            nodes = []
            for entry in await service.get_branch(session_id, leaf_id):
                node = entry.messages[0]
                if isinstance(node, ToolResultMessage):
                    nodes.append(
                        {
                            "entry_id": entry.id,
                            "role": "toolResult",
                            "tool_name": node.tool_name,
                            "tool_call_id": node.tool_call_id,
                        }
                    )
                else:
                    nodes.append({"entry_id": entry.id, "role": node.role})
            return nodes

        holder = {"request_entry_id": run.request_entry_id}
        holder["business_date"] = business_date(request_entry.created_at)

        def context_for(source_entry_id: str) -> BusinessContext:
            return BusinessContext(
                timezone="Asia/Shanghai",
                business_date=holder["business_date"],
                session_id=session_id,
                run_id=run.id,
                request_entry_id=holder["request_entry_id"],
                source_entry_id=source_entry_id,
            )

        def bind_tools(source_entry_id: str) -> dict[str, AgentTool]:
            return bind_business_tools(
                business, context_for(source_entry_id), call, prepared
            )

        async def transform_context(messages, signal):
            return [
                *messages,
                SystemMessage(
                    role="system",
                    content="",
                    sections={
                        "business_context": json.dumps(
                            {
                                "timezone": "Asia/Shanghai",
                                "business_date": holder["business_date"],
                                "message_nodes": await message_nodes(position["id"]),
                            },
                            ensure_ascii=False,
                        )
                    },
                    timestamp=time_ns() // 1_000_000,
                ),
            ]

        captured: list = []
        turns = [
            assistant(
                [
                    ToolCall(
                        type="toolCall",
                        id="call-1",
                        name="prepare_profile_update",
                        arguments={
                            "profile_id": 1,
                            "base_profile_version": None,
                            "payload": dict(FULL_PROFILE),
                        },
                    )
                ],
                "toolUse",
            ),
            assistant([TextContent(type="text", text="已整理完整画像，等待你的确认。")], "stop"),
        ]
        config = AgentLoopConfig(
            model=load_model_config(),
            max_turns=8,
            transform_context=transform_context,
            save_message=save_message,
            bind_tools=bind_tools,
        )
        context = {
            "messages": branch[:-1],
            "tools": bind_business_tools(
                business, context_for(run.request_entry_id), call, prepared
            ),
        }
        messages = await run_agent_loop(
            [branch[-1]],
            context,
            config,
            _noop_emit,
            stream_fn=scripted_stream(turns, captured),
        )

        results = [
            message for message in messages if isinstance(message, ToolResultMessage)
        ]
        assert len(results) == 1, results
        result = results[0]
        assert result.tool_name == "prepare_profile_update"
        assert result.is_error is False, result.content[0].text
        proposal = json.loads(result.content[0].text)
        assert set(proposal) == {
            "proposal_id",
            "profile_id",
            "base_profile_version",
            "payload",
        }, proposal
        assert proposal["profile_id"] == 1
        assert proposal["payload"] == FULL_PROFILE

        # 每批绑定：source_entry_id 为已保存助手节点，request_entry_id 为发起请求的用户节点。
        source_entry_id = saved[0]
        display_entry_id = saved[1]
        assert prepared == {"call-1": proposal["proposal_id"]}, prepared
        snapshot = await repository.get_snapshot(proposal["proposal_id"])
        assert snapshot is not None
        assert snapshot.session_id == session_id
        assert snapshot.request_entry_id == run.request_entry_id
        assert snapshot.source_entry_id == source_entry_id
        assert snapshot.display_entry_id == display_entry_id
        assert snapshot.confirmation_entry_id is None
        assert snapshot.status == "pending"
        assert (await business.get_profile()).version is None

        # 临时日期与消息节点引用进入模型请求投影，保持原系统提示词完整。
        prompt = get_current_system_prompt(captured[0])
        assert SYSTEM_PROMPT in prompt
        assert '"business_date"' in prompt and holder["business_date"] in prompt
        assert '"timezone"' in prompt and "Asia/Shanghai" in prompt
        assert '"message_nodes"' in prompt
        assert run.request_entry_id in prompt
        assert len(captured) == 2, captured
        # 第二批投影携带助手节点与已绑定的工具结果节点，保存工具据此取值。
        second_prompt = get_current_system_prompt(captured[1])
        assert source_entry_id in second_prompt
        assert display_entry_id in second_prompt
        assert '"prepare_profile_update"' in second_prompt

        history = await service.get_session_history(session_id)
        assert set(history.model_dump()) == {"session", "entries", "runs", "steering"}
        assert [entry.messages[0].role for entry in history.entries][1:] == [
            "user",
            "assistant",
            "toolResult",
            "assistant",
        ]
        return {
            "proposal_id": proposal["proposal_id"],
            "source_entry_id": source_entry_id,
            "display_entry_id": display_entry_id,
            "business_date": holder["business_date"],
        }
    finally:
        await database.close()


async def _noop_emit(event) -> None:
    return None


def check() -> None:
    evidence = temporary_root("business-loop")
    evidence.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=evidence, ignore_cleanup_errors=True) as directory:
        result = asyncio.run(check_loop(Path(directory)))
    (evidence / "business-loop.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "画像工具按批绑定、稳定声明与日期上下文注入检查通过："
        + json.dumps(result, ensure_ascii=False)
    )


if __name__ == "__main__":
    check()
