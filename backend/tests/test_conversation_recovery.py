# 阶段 7 测试：重启与异常恢复——启动收敛遗留 Run、保留已提交片段、排除不完整 Assistant、下一轮可继续。
# 依据：CHAT_HISTORY_IMPLEMENTATION_PLAN.md §4 阶段 7、§5 验收场景 8／9；
#       Pi agent-session.ts:689-708（message_end 才持久化消息）、transform-messages.ts:196-206
#       （error／aborted Assistant 保留在历史里但从模型输入中跳过）；死锁约束：事务内只调 *_in_transaction。

import json
from pathlib import Path
from typing import Any

from test_conversation_routes import _client, _create_conversation, _run

from api.app import INTERRUPTED_RUN_ERROR_CODE, create_app
from config import database_path
from domain.conversations.repo import ConversationRepo
from graph.router import ROUTER_SYSTEM_PROMPT, FitnessIntent
from graph.workflow import GENERAL_CHAT_SYSTEM_PROMPT
from storage.db import Database

CHAT_ID = "11111111-1111-1111-1111-111111111111"
THREAD_RUNNING = "21111111-1111-1111-1111-111111111111"
THREAD_PENDING = "31111111-1111-1111-1111-111111111111"
THREAD_WAITING = "41111111-1111-1111-1111-111111111111"
THREAD_COMPLETED = "51111111-1111-1111-1111-111111111111"
THREAD_NEXT = "61111111-1111-1111-1111-111111111111"
CREATED_AT = "2026-06-01T09:00:00+00:00"
RESTARTED_AT = "2026-06-02T00:00:00+00:00"
CRASH_REQUEST = "第一轮：深蹲怎么安排"
FRAGMENT = "第一轮回答片段：先做深蹲"
SECOND_REQUEST = "第二轮：改成周五"
SECOND_ANSWER = "第二轮回答：已按周五日程理解"


async def _begin(
    db: Database, *, conversation_id: str, run_id: str, thread_id: str, request: str
) -> None:
    """用户 Entry 与 ``pending`` Run 同事务落库：与 ``/api/agent/run`` 的写序一致。"""
    conversations = ConversationRepo(db)
    async with db.transaction() as conn:
        await conversations.begin_run_in_transaction(
            conn,
            conversation_id=conversation_id,
            run_id=run_id,
            thread_id=thread_id,
            client_request_id=f"request-{run_id}",
            entry_id=f"entry-{run_id}",
            content=request,
            created_at=CREATED_AT,
        )


async def _crash_mid_stream(
    db: Database,
    *,
    conversation_id: str,
    run_id: str,
    thread_id: str,
    request: str,
    fragment: str,
) -> None:
    """复现进程中途终止：Run 停在 ``running``，已推送的 ``message`` 片段留在 Event 里。

    硬终止不执行任何处理分支：Run 的状态与事件由启动收敛接手；客户端断开走
    ``asyncio.CancelledError`` 分支，由取消语义自身收敛为 ``cancelled``。
    """
    conversations = ConversationRepo(db)
    await _begin(
        db,
        conversation_id=conversation_id,
        run_id=run_id,
        thread_id=thread_id,
        request=request,
    )
    async with db.transaction() as conn:
        await conversations.update_run_status_in_transaction(
            conn, run_id, status="running", updated_at=CREATED_AT
        )
        await conversations.append_event_in_transaction(
            conn,
            run_id=run_id,
            sequence=1,
            event_type="message",
            payload={"text": fragment},
            created_at=CREATED_AT,
        )


def _event_texts(round_: dict[str, Any]) -> list[tuple[str, str]]:
    """展示投影里的片段：Event 名与可见文本。"""
    return [(event["event"], event["data"]["text"]) for event in round_["events"]]


async def test_application_startup_converges_interrupted_runs_and_keeps_events(
    tmp_path: Path,
) -> None:
    """启动收敛：遗留 ``pending``／``running`` 标 failed，Event 与 waiting／completed 原样保留。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(database_path(data_dir))
    await db.open()
    await db.migrate()
    try:
        conversations = ConversationRepo(db)
        await conversations.create_conversation(
            conversation_id=CHAT_ID, title="重启收敛", created_at=CREATED_AT
        )
        await _crash_mid_stream(
            db,
            conversation_id=CHAT_ID,
            run_id="run-running",
            thread_id=THREAD_RUNNING,
            request=CRASH_REQUEST,
            fragment=FRAGMENT,
        )
        await _begin(
            db,
            conversation_id=CHAT_ID,
            run_id="run-pending",
            thread_id=THREAD_PENDING,
            request="模型调用前即中断",
        )
        await _begin(
            db,
            conversation_id=CHAT_ID,
            run_id="run-waiting",
            thread_id=THREAD_WAITING,
            request="停在确认等待",
        )
        async with db.transaction() as conn:
            await conversations.update_run_status_in_transaction(
                conn, "run-waiting", status="running", updated_at=CREATED_AT
            )
            await conversations.update_run_with_event_in_transaction(
                conn,
                "run-waiting",
                event_type="waiting",
                sequence=1,
                payload={"draft_plan_id": 7},
                status="waiting",
                created_at=CREATED_AT,
            )
        await _begin(
            db,
            conversation_id=CHAT_ID,
            run_id="run-completed",
            thread_id=THREAD_COMPLETED,
            request="已完成的一轮",
        )
        async with db.transaction() as conn:
            await conversations.update_run_status_in_transaction(
                conn, "run-completed", status="running", updated_at=CREATED_AT
            )
            await conversations.finish_run_in_transaction(
                conn,
                "run-completed",
                sequence=1,
                payload={"text": "已完成回答"},
                content="已完成回答",
                entry_id="entry-run-completed-assistant",
                created_at=CREATED_AT,
            )
    finally:
        await db.close()

    app = create_app(data_dir=data_dir, frontend_dist=data_dir)
    async with app.router.lifespan_context(app):
        conversations = ConversationRepo(app.state.db)
        running = await conversations.read_run("run-running")
        pending = await conversations.read_run("run-pending")
        assert running is not None and pending is not None
        assert running.status == "failed"
        assert running.error_code == INTERRUPTED_RUN_ERROR_CODE
        assert running.assistant_entry_id is None
        assert pending.status == "failed"
        assert pending.error_code == INTERRUPTED_RUN_ERROR_CODE

        # 确认等待与已完成轮次不属于异常恢复范围：重启后确认卡照旧可恢复。
        assert (await conversations.read_run("run-waiting")).status == "waiting"
        assert (await conversations.read_run("run-completed")).status == "completed"

        events = await conversations.list_run_events("run-running")
        assert [
            (event.sequence, event.event_type, event.payload["text"]) for event in events
        ] == [(1, "message", FRAGMENT)]
        assert await conversations.list_run_events("run-waiting") != ()
        # 未完成片段只留在 Event：没有变成 Assistant Entry，模型上下文投影不到它。
        assert len(await conversations.list_entries(CHAT_ID)) == 5


async def test_interrupted_run_stays_visible_and_the_next_turn_excludes_the_fragment(
    tmp_path: Any,
) -> None:
    """恢复后仍能展示片段与未完成状态，下一轮只收到完整历史并正常完成。"""
    async with _client(
        tmp_path,
        structured={FitnessIntent: [{"domain": "general", "action": "chat"}]},
        text={GENERAL_CHAT_SYSTEM_PROMPT: [SECOND_ANSWER]},
    ) as (client, model, db):
        chat_id = await _create_conversation(client, "会话重启")
        await _crash_mid_stream(
            db,
            conversation_id=chat_id,
            run_id="run-interrupted",
            thread_id=THREAD_RUNNING,
            request=CRASH_REQUEST,
            fragment=FRAGMENT,
        )
        conversations = ConversationRepo(db)
        assert (await conversations.read_run("run-interrupted")).status == "running"

        interrupted = (await client.get(f"/api/conversations/{chat_id}")).json()[
            "rounds"
        ][0]
        assert interrupted["status"] == "running"
        assert interrupted["request"] == CRASH_REQUEST
        assert interrupted["assistants"] == []
        assert _event_texts(interrupted) == [("message", FRAGMENT)]

        assert (
            await conversations.converge_unfinished_runs(
                error_code=INTERRUPTED_RUN_ERROR_CODE, updated_at=RESTARTED_AT
            )
            == 1
        )
        recovered = (await client.get(f"/api/conversations/{chat_id}")).json()["rounds"][0]
        assert recovered["status"] == "failed"
        assert recovered["assistants"] == []
        assert _event_texts(recovered) == [("message", FRAGMENT)]

        frames = await _run(
            client,
            chat_id=chat_id,
            thread_id=THREAD_NEXT,
            request=SECOND_REQUEST,
            client_request_id="request-next",
        )
        assert [name for name, _data in frames][-1] == "done"
        for prompt in (ROUTER_SYSTEM_PROMPT, GENERAL_CHAT_SYSTEM_PROMPT):
            payload = json.loads(
                [text for name, text in model.calls if name == prompt][-1]
            )
            assert payload["conversation_messages"] == [
                {"role": "user", "text": CRASH_REQUEST}
            ]

        rounds = (await client.get(f"/api/conversations/{chat_id}")).json()["rounds"]
        assert [round_["status"] for round_ in rounds] == ["failed", "completed"]
        assert [round_["request"] for round_ in rounds] == [CRASH_REQUEST, SECOND_REQUEST]
        assert [
            (assistant["content"], assistant["status"])
            for assistant in rounds[1]["assistants"]
        ] == [(SECOND_ANSWER, "complete")]
        assert (await conversations.read_run("run-interrupted")).assistant_entry_id is None
