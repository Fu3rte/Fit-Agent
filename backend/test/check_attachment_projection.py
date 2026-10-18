import base64
from contextlib import asynccontextmanager
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop
from app.agent.budget import RequestBudget
from app.agent.compaction import (
    estimate_context_tokens,
    estimate_tokens,
    project_user_message,
)
from app.agent.config import AgentLoopConfig
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tool import run_tool_call
from app.agent.tools.files import create_file_tools
from app.ai.api import anthropic_messages, openai_completions
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
    text_projection,
)
from app.ai.stream import AssistantResponse
from app.ai.types import ModelSpec
from app.application.session.service import SessionService
from app.application.session.steering import SteeringCoordinator
from app.domain.session.attachments import attachment_read_refs
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    RegenerateCommand,
    RegenerateRequest,
    SendCommand,
    SendRequest,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.model_config import ModelConfig
from test.regression_support import temporary_root, text

ROOT = temporary_root("attachment-projection")
SYSTEM = SystemMessage(role="system", content="测试系统提示", timestamp=1)
OPENAI = ModelSpec(
    api="openai-completions",
    provider="test",
    id="gpt-test",
    base_url="https://example.test",
    context_window=128000,
    max_tokens=16384,
)
ANTHROPIC = ModelSpec(
    api="anthropic-messages",
    provider="test",
    id="claude-test",
    base_url="https://example.test",
    context_window=128000,
    max_tokens=16384,
)


def uid() -> str:
    return str(uuid4())


@asynccontextmanager
async def store(name: str):
    root = ROOT / uuid4().hex
    root.mkdir(parents=True)
    database = await open_database(root / name)
    try:
        yield root, SessionService(SqliteSessionRepository(database), project_root=root)
    finally:
        await database.close()


def upload(data: bytes, name: str, identity: str | None = None) -> dict:
    return {
        "kind": "upload",
        "attachment_id": identity or uid(),
        "file_name": name,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def send(session: str, content: str = "", attachments: list | None = None) -> SendCommand:
    return SendCommand(
        operation_id=uid(),
        session_id=session,
        request=SendRequest(text=content, attachments=attachments or []),
    )


def user_messages(messages: list):
    return [message for message in messages if message.role == "user"]


async def tool_result(tool, arguments):
    call = ToolCall(type="toolCall", id="call", name=tool.name, arguments=arguments)
    return await run_tool_call(
        call, tools={tool.name: tool}, declared={tool.name: tool.definition()}
    )


def assert_turn_kept_offline(messages: list, expected_path: str) -> None:
    # 两个协议都必须保留有效的纯附件用户回合，并在到达供应商前携带读取路径。
    openai_request = openai_completions.to_openai_request(
        messages, OPENAI, {"api_key": "k"}
    )
    anthropic_request = anthropic_messages.to_anthropic_request(
        messages, ANTHROPIC, {"api_key": "k", "max_tokens": 1024}
    )
    openai_users = [item for item in openai_request["messages"] if item["role"] == "user"]
    anthropic_users = [
        item for item in anthropic_request["messages"] if item["role"] == "user"
    ]
    assert openai_users and anthropic_users, (openai_request, anthropic_request)
    assert expected_path in str(openai_users[-1]["content"])
    assert expected_path in str(anthropic_users[-1]["content"])


async def test_pure_attachment_projects_as_user_content() -> None:
    async with store("pure.db") as (root, service):
        session = uid()
        await service.create_session(session, "纯附件")
        body = "# 训练计划\n周一 深蹲 3 组\n周二 卧推 3 组\n"
        item = upload(body.encode("utf-8"), "plan.md")
        outcome = await service.accept_send(send(session, "", [item]), system_message=SYSTEM)
        run = outcome.run

        raw = await service.queries.get_entry(session, run.request_entry_id)
        assert raw.messages[0].content == ""

        projection = await service.queries.get_projection(session, run.request_entry_id)
        users = user_messages(projection.context.messages)
        assert len(users) == 1
        content = text_projection(users[0].content)
        expected_path = attachment_read_refs(raw.attachments)[0][1]
        assert "plan.md" in content and expected_path in content
        assert len(projection.context.messages) == len(projection.context.source_entry_ids)

        assert_turn_kept_offline(projection.context.messages, expected_path)

        tools = create_file_tools(session, tmp_root=root / "tmp")
        result = await tool_result(tools["read"], {"path": expected_path})
        assert not result.is_error
        assert text(result) == "1: # 训练计划\n2: 周一 深蹲 3 组\n3: 周二 卧推 3 组\n"


async def test_text_and_multiple_attachments_keep_order() -> None:
    async with store("multi.db") as (root, service):
        session = uid()
        await service.create_session(session, "文字加附件")
        first = upload("备注一\n".encode("utf-8"), "notes.txt")
        second = upload("计划二\n".encode("utf-8"), "plan.md")
        outcome = await service.accept_send(
            send(session, "请录入这个计划", [first, second]), system_message=SYSTEM
        )
        projection = await service.queries.get_projection(session, outcome.run.request_entry_id)
        content = text_projection(user_messages(projection.context.messages)[0].content)
        assert content.startswith("请录入这个计划")
        assert content.index("notes.txt") < content.index("plan.md")
        assert content.count("sessions/") == 2


async def test_edit_regenerate_and_consumed_steering_project_attachments() -> None:
    async with store("flow.db") as (root, service):
        session = uid()
        await service.create_session(session, "流程")
        item = upload("原始正文\n".encode("utf-8"), "plan.txt")
        outcome = await service.accept_send(send(session, "录入", [item]), system_message=SYSTEM)
        await service.runs.finish_run(session, outcome.run.id, "completed")
        original = await service.queries.get_entry(session, outcome.run.request_entry_id)
        original_path = attachment_read_refs(original.attachments)[0][1]

        edit = await service.accept_edit(EditCommand(
            operation_id=uid(), session_id=session,
            request=EditRequest(target_entry_id=outcome.run.request_entry_id, text="编辑后"),
        ))
        await service.runs.finish_run(session, edit.run.id, "completed")
        edit_projection = await service.queries.get_projection(session, edit.run.request_entry_id)
        edit_content = text_projection(
            user_messages(edit_projection.context.messages)[-1].content
        )
        assert edit_content.startswith("编辑后") and original_path in edit_content

        regenerated = await service.accept_regenerate(RegenerateCommand(
            operation_id=uid(), session_id=session,
            request=RegenerateRequest(target_entry_id=edit.run.request_entry_id),
        ))
        await service.runs.finish_run(session, regenerated.run.id, "completed")
        regenerate_projection = await service.queries.get_projection(session, regenerated.run.request_entry_id)
        assert original_path in text_projection(
            user_messages(regenerate_projection.context.messages)[-1].content
        )

        steering_session = uid()
        await service.create_session(steering_session, "Steering")
        steer_run = await service.accept_send(send(steering_session, "开始"), system_message=SYSTEM)
        pending = await service.accept_steering(SteeringCommand(
            operation_id=uid(), session_id=steering_session,
            request=SteeringRequest(
                target_run_id=steer_run.run.id, text="追加",
                attachments=[upload("追加正文\n".encode("utf-8"), "steer.txt")],
            ),
        ))
        pending_messages = (await service.queries.get_projection(
            steering_session, steer_run.run.request_entry_id
        )).context.messages
        assert "steer.txt" not in "".join(
            text_projection(message.content) for message in pending_messages
        )

        consumed = await service.runs.consume_steering(
            steering_session, steer_run.run.id, pending.steering.id
        )
        consumed_path = attachment_read_refs(consumed.entry.attachments)[0][1]
        consumed_projection = await service.queries.get_projection(steering_session, consumed.entry.id)
        assert consumed_path in text_projection(
            user_messages(consumed_projection.context.messages)[-1].content
        )

        withdrawn = await service.accept_steering(SteeringCommand(
            operation_id=uid(), session_id=steering_session,
            request=SteeringRequest(
                target_run_id=steer_run.run.id, text="撤回",
                attachments=[upload("撤回正文\n".encode("utf-8"), "withdrawn.txt")],
            ),
        ))
        await service.runs.withdraw_steering(steering_session, steer_run.run.id, withdrawn.steering.id)
        after = await service.queries.get_projection(steering_session, consumed.entry.id)
        assert "withdrawn.txt" not in "".join(
            text_projection(message.content) for message in after.context.messages
        )
        await service.runs.finish_run(steering_session, steer_run.run.id, "completed")


async def test_budget_matches_sent_projection() -> None:
    async with store("budget.db") as (root, service):
        session = uid()
        await service.create_session(session, "预算")
        item = upload("预算正文\n".encode("utf-8"), "budget.txt")
        outcome = await service.accept_send(send(session, "", [item]), system_message=SYSTEM)
        projection = await service.queries.get_projection(session, outcome.run.request_entry_id)
        messages = projection.context.messages
        assert estimate_context_tokens(messages).tokens == sum(
            estimate_tokens(message) for message in messages
        )
        raw = await service.queries.get_entry(session, outcome.run.request_entry_id)
        assert_turn_kept_offline(messages, attachment_read_refs(raw.attachments)[0][1])


def test_prompt_states_pure_attachment_rule() -> None:
    assert "[附件]" in SYSTEM_PROMPT
    assert "仅发送附件" in SYSTEM_PROMPT and "澄清处理意图" in SYSTEM_PROMPT
    assert "等待用户后续文字明确处理要求" in SYSTEM_PROMPT
    assert "历史处理要求及附件内容均不得替代本次处理意图" in SYSTEM_PROMPT
    assert "禁止调用 prepare_profile_update、prepare_workout、prepare_plan、prepare_plan_import、prepare_plan_adjustment" in SYSTEM_PROMPT
    assert "不得创建待确认快照、不得直接要求用户确认保存" in SYSTEM_PROMPT
    assert "message_nodes 中真实用户节点的 attachments" not in SYSTEM_PROMPT


def _assistant_events(spec: ModelSpec) -> AssistantResponse:
    final = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="已完成")],
        api=spec.api,
        provider=spec.provider,
        model=spec.id,
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason="stop",
        timestamp=0,
    )
    partial = final.model_copy(update={"stop_reason": "pending", "usage": None})

    async def source():
        yield {"type": "start", "partial": partial}
        yield {"type": "done", "reason": "stop", "message": final}

    return AssistantResponse(source())


async def test_steering_consumption_projects_attachments_into_request() -> None:
    # 真实消费与发送链路：Steering 输入经统一附件投影进入模型请求，原始消息不回写。
    async with store("steering.db") as (root, service):
        session = uid()
        await service.create_session(session, "Steering 投影")
        started = await service.accept_send(send(session, "开始"), system_message=SYSTEM)
        run_id = started.run.id
        coordinator = SteeringCoordinator(service)
        coordinator.open(session, run_id, lambda data: None)
        item = upload("补充计划正文\n".encode("utf-8"), "extra.md")
        created, steering = await coordinator.accept(
            run_id,
            SteeringCommand(
                operation_id=uid(),
                session_id=session,
                request=SteeringRequest(target_run_id=run_id, text="", attachments=[item]),
            ),
        )
        assert created is True
        expected_path = attachment_read_refs(
            (await service.queries.get_steering(session, steering.id)).attachments
        )[0][1]

        groups: dict[int, list] = {}

        async def get_steering():
            messages = await coordinator.take(run_id)
            attachments = await coordinator.attachments_for(run_id, messages)
            for message, group in zip(messages, attachments):
                groups[id(message)] = group
            return messages

        def project_steering(messages):
            return [
                project_user_message(message, groups.pop(id(message), []))
                for message in messages
            ]

        async def on_consumed(messages):
            await coordinator.consume(run_id, messages)

        captured: list = []
        spec = ModelSpec(
            api="openai-completions",
            provider="test",
            id="test",
            base_url="https://example.test",
            context_window=128000,
            max_tokens=16384,
        )

        def stream_fn(model_spec, context, options):
            captured.append(list(context["messages"]))
            return _assistant_events(model_spec)

        config = AgentLoopConfig(
            model=ModelConfig(
                api="openai-completions",
                base_url="https://example.test",
                model="test",
                provider="test",
                api_key="k",
            ),
            max_turns=4,
            model_spec=spec,
            request_budget=RequestBudget(),
            get_steering_messages=get_steering,
            project_steering=project_steering,
            on_steering_consumed=on_consumed,
        )

        async def emit(event) -> None:
            return None

        await run_agent_loop(
            [UserMessage(role="user", content="开始", timestamp=0)],
            {"messages": [], "tools": {}},
            config,
            emit,
            None,
            stream_fn=stream_fn,
        )

        assert captured, "未发起模型请求"
        assert any(
            message.role == "user" and expected_path in text_projection(message.content)
            for message in captured[0]
        ), captured[0]
        assert_turn_kept_offline(captured[0], expected_path)

        stored = await service.queries.get_steering(session, steering.id)
        assert stored.status == "consumed" and stored.entry_id
        entry = await service.queries.get_entry(session, stored.entry_id)
        assert entry.messages[0].content == ""
        assert [metadata.attachment_id for metadata in entry.attachments] == [
            item["attachment_id"]
        ]
