import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.agent.agent_loop import run_agent_loop
from app.agent.budget import RequestBudget
from app.agent.config import AgentLoopConfig
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tool import run_tool_call
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.ai.messages import (
    SystemMessage,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from app.model_config import ModelConfig
from test.check_business_loop import assistant, scripted_stream
from test.check_structured_confirmation import Fixture, new_id, now_ms
from test.regression_support import temporary_root

EVIDENCE = temporary_root("agent-save-boundary")
# 方案 §7.7：模型可用注册表不提供这四项保存工具。
SAVE_TOOLS = ("save_profile_update", "save_plan", "save_workout", "update_workout")
# 准备、业务查询与原结果状态查询继续可用。
QUERY_AND_PREPARE = (
    "get_profile", "prepare_profile_update", "get_profile_update_status",
    "get_workout", "list_workouts", "prepare_workout", "get_workout_save_status",
    "get_current_plan", "get_plan", "list_plans", "prepare_plan", "get_plan_save_status",
    "get_pending_proposal", "prepare_plan_import", "prepare_plan_adjustment",
    "search_exercises", "calculate_date",
)
DECLARED = {item.name: item for item in business_tool_declarations()}
PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}


def text(message) -> str:
    return "".join(block.text for block in message.content)


def body(message) -> dict:
    return json.loads(text(message))


def call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(type="toolCall", id="call-" + uuid4().hex[:8], name=name, arguments=arguments)


def model() -> ModelConfig:
    return ModelConfig(
        api="openai-completions", base_url="http://localhost", model="test",
        provider="test", api_key="test-key",
    )


def check_declarations() -> dict:
    names = [item.name for item in business_tool_declarations()]
    assert not (set(SAVE_TOOLS) & set(names)), sorted(set(SAVE_TOOLS) & set(names))
    assert set(QUERY_AND_PREPARE) <= set(names), sorted(set(QUERY_AND_PREPARE) - set(names))
    return {"declared": names, "save_absent": list(SAVE_TOOLS)}


def check_prompt() -> dict:
    for name in SAVE_TOOLS:
        assert name not in SYSTEM_PROMPT, name
    required = (
        "确认按钮", "不构成保存授权", "重新准备", "模型不执行",
        "get_profile_update_status", "get_workout_save_status", "get_plan_save_status",
    )
    missing = [phrase for phrase in required if phrase not in SYSTEM_PROMPT]
    assert not missing, missing
    return {"save_absent": list(SAVE_TOOLS), "phrases": list(required)}


async def check_registry_rejection(f: Fixture, trusted) -> dict:
    session = await f.session()
    run = await f.turn(session, "普通确认消息")
    registry = bind_business_tools(f.business, f.context(session, run.request_entry_id, run.request_entry_id), trusted, {}, {}, {})
    assert not (set(SAVE_TOOLS) & set(registry)), sorted(set(SAVE_TOOLS) & set(registry))
    before = f.database.connection.total_changes
    after_ids: list[str] = []

    async def after(ctx, signal):
        after_ids.append(ctx.tool_call.id)

    reasons = {}
    for name in SAVE_TOOLS:
        message = await run_tool_call(
            call(name, {"proposal_id": new_id(), "display_entry_id": new_id(),
                        "confirmation_entry_id": new_id()}),
            tools=registry, declared=DECLARED, after_tool_call=after,
        )
        assert message.is_error and "工具不存在" in text(message), (name, text(message))
        reasons[name] = text(message)
    assert after_ids == [], after_ids
    assert f.database.connection.total_changes == before
    return {"registry_absent": True, "reasons": reasons, "unchanged": True}


async def check_queries_and_prepare(f: Fixture, trusted) -> dict:
    session = await f.session()
    run = await f.turn(session, "请整理我的画像", finish=False)
    arguments = {"profile_id": 1, "base_profile_version": None, "payload": dict(PROFILE)}
    source = await f.append(session, run.id, run.request_entry_id,
                            assistant([call("prepare_profile_update", arguments)], "toolUse"))
    await f.service.finish_run(session, run.id, "completed")
    prepared: dict[str, str] = {}
    registry = bind_business_tools(
        f.business, f.context(session, run.request_entry_id, source), trusted, prepared, {}, {})

    async def invoke(name, args):
        return await run_tool_call(
            call(name, args), tools={name: registry[name]}, declared={name: DECLARED[name]})

    empty = await invoke("get_profile", {})
    assert not empty.is_error and body(empty) == {"version": None, "content": None}

    proposal = await invoke("prepare_profile_update", arguments)
    assert not proposal.is_error, text(proposal)
    parsed = body(proposal)
    assert list(prepared.values()) == [parsed["proposal_id"]], prepared

    status = await invoke("get_profile_update_status", {"proposal_id": parsed["proposal_id"]})
    assert not status.is_error and body(status)["status"] == "pending", text(status)
    return {"prepare_ok": parsed["proposal_id"], "status": body(status)["status"]}


async def check_old_declaration(f: Fixture, trusted) -> dict:
    # 历史会话保留原始工具声明：注册表不再包含保存工具，运行不因此中断。
    old_tool = Tool(
        name="save_profile_update", description="旧版保存工具声明",
        parameters={"type": "object", "additionalProperties": False, "properties": {}},
    )
    system = SystemMessage(
        role="system", content=SYSTEM_PROMPT,
        tools_added=[*business_tool_declarations(), old_tool], timestamp=now_ms(),
    )
    session = await f.session()
    run = await f.turn(session, "确认。")
    registry = bind_business_tools(f.business, f.context(session, run.request_entry_id, run.request_entry_id), trusted, {}, {}, {})
    before = f.database.connection.total_changes
    config = AgentLoopConfig(
        model=model(), max_turns=4, request_budget=RequestBudget(),
        bind_tools=lambda _source: registry,
    )
    turns = [
        assistant([call("save_profile_update", {"proposal_id": new_id(), "display_entry_id": new_id(),
                                                "confirmation_entry_id": run.request_entry_id})], "toolUse"),
        assistant([TextContent(type="text", text="聊天文字不构成保存授权。")], "stop"),
    ]
    messages = await run_agent_loop(
        [UserMessage(role="user", content="确认。", timestamp=now_ms())],
        {"messages": [system], "tools": registry}, config, noop_emit,
        stream_fn=scripted_stream(turns, []),
    )
    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    assert len(results) == 1, results
    assert results[0].is_error and "工具不存在" in text(results[0]), text(results[0])
    assert f.database.connection.total_changes == before
    assert (await f.business.get_profile()).version is None
    return {"rejected": text(results[0]), "unchanged": True}


async def noop_emit(event) -> None:
    return None


async def run(root: Path) -> dict:
    f = await Fixture(root / "boundary.db").seeded()
    loop = asyncio.get_running_loop()

    def trusted(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    try:
        return {
            "declarations": check_declarations(),
            "prompt": check_prompt(),
            "registry_rejection": await check_registry_rejection(f, trusted),
            "queries_and_prepare": await check_queries_and_prepare(f, trusted),
            "old_declaration": await check_old_declaration(f, trusted),
        }
    finally:
        await f.close()


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        evidence = asyncio.run(run(Path(directory)))
    (EVIDENCE / "agent-save-boundary.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(
        "PASS: 模型声明与执行注册表均无四项保存工具；保存工具请求被拒绝且零业务改动；"
        "旧会话声明不恢复保存能力且不中断运行；准备与只读查询正常"
    )


if __name__ == "__main__":
    check()
