"""Stage 3 子任务 03：LangGraph SQLite Checkpointer 的独立存档与连接生命周期。

依据：``refactor-log/stage3.md`` §5／§2.3／§7「Checkpoint」；讨论总结 §5.1／§9；REFACTOR_PLAN §5.6。

覆盖：saver 用已安装的 ``langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver``（不自研 checkpoint repo、
不写业务迁移），checkpoint 表由 saver 自己建在独立 SQLite 文件；连接在异步生命周期内开、关；固定
conversation id 下的真实 interrupt 落盘后，关闭连接并重开仍能按同一 thread 定位等待确认的节点与
fixture draft 身份；不同 thread 的状态不混用；恢复读取不激活、不归档计划，也不改动原 active 计划；
测试图只有固定节点与固定 State（无模型节点）；无 API Key 时非模型业务照常启动，Provider 配置的名称
与取值都不进入 State 与存档文件。

测试只使用固定最小图（固定节点、固定 State）与 pytest ``tmp_path`` 临时文件，不调真实模型、不需要
API Key；业务计划事实按要求用直接 SQL 写入 fixture（正式创建／激活入口留 Stage 5）。
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from api.app import create_app
from config import (
    CHECKPOINT_DATABASE_FILENAME,
    MODEL_API_KEY_ENV,
    MODEL_BASE_URL_ENV,
    MODEL_MODEL_ENV,
    checkpoint_database_path,
    database_path,
)
from graph.checkpointer import open_checkpointer, thread_config
from graph.state import WorkflowState
from storage.db import Database

#: 固定 conversation id；它同时就是 Checkpointer 的 thread id（不维护第二套映射）。
CONVERSATION_ID = "stage3-checkpoint-conversation"
OTHER_CONVERSATION_ID = "stage3-checkpoint-other-conversation"
ABSENT_CONVERSATION_ID = "stage3-checkpoint-absent-conversation"
CREATED_AT = "2026-06-01T08:00:00+08:00"

#: 等待确认时暂停的节点名，与讨论总结 §4.2／REFACTOR_PLAN §9.2 的计划子图同名（此处只是最小测试图）。
WAITING_NODE = "wait_for_confirmation"


# ---------- 固定最小图（不是正式确认工作流：确认／激活事务留 Stage 5） ----------


def _stage_draft(state: WorkflowState) -> WorkflowState:
    """把 fixture draft 身份写进 State：不生成计划、不写业务库。"""
    return {"draft_plan_id": state["draft_plan_id"], "confirmation": "pending"}


def _wait_for_confirmation(state: WorkflowState) -> WorkflowState:
    """真实 interrupt：图在等待确认处暂停，落盘的 State 即「等待确认」的工作记忆。"""
    interrupt({"draft_plan_id": state["draft_plan_id"]})
    return {}


def _pausing_graph(saver: AsyncSqliteSaver):
    """冻结 State 上的固定最小图：``stage_draft → wait_for_confirmation``（真实 interrupt）。"""
    builder = StateGraph(WorkflowState)
    builder.add_node("stage_draft", _stage_draft)
    builder.add_node(WAITING_NODE, _wait_for_confirmation)
    builder.add_edge(START, "stage_draft")
    builder.add_edge("stage_draft", WAITING_NODE)
    builder.add_edge(WAITING_NODE, END)
    return builder.compile(checkpointer=saver)


def _initial_state(conversation_id: str, draft_plan_id: int) -> WorkflowState:
    return {
        "conversation_id": conversation_id,
        "request": "生成计划",
        "intent": "generate_plan",
        "draft_plan_id": draft_plan_id,
        "revision_count": 0,
    }


# ---------- fixture 业务事实（直接 SQL；正式写入口留 Stage 5） ----------


async def _insert_plan(db: Database, *, version: int, status: str) -> int:
    """写入一条 fixture 计划行，返回计划身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, structured_content, created_at,"
            " confirmed_at) VALUES (?, ?, '{}', ?, ?)",
            (
                version,
                status,
                CREATED_AT,
                CREATED_AT if status == "active" else None,
            ),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _plan_facts(db: Database, plan_id: int) -> tuple[str, str | None]:
    """计划的持久事实：状态与归档时间（恢复读取不得改动它们）。"""

    async def op(conn) -> tuple[str, str | None]:
        async with conn.execute(
            "SELECT status, archived_at FROM plans WHERE id = ?", (plan_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return (row["status"], row["archived_at"])

    return await db.under_lock(op)


async def _active_plan_ids(db: Database) -> tuple[int, ...]:
    async def op(conn) -> tuple[int, ...]:
        async with conn.execute(
            "SELECT id FROM plans WHERE status = 'active' ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(row["id"] for row in rows)

    return await db.under_lock(op)


@asynccontextmanager
async def _serving(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


def _checkpoint_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


async def _assert_saver_closed(saver: AsyncSqliteSaver) -> None:
    """通过公开数据库操作确认连接已关闭，不依赖 aiosqlite 内部属性。"""
    with pytest.raises(ValueError):
        await saver.conn.execute("SELECT 1")


# ---------- 生命周期与文件隔离 ----------


async def test_lifespan_opens_and_closes_the_checkpoint_saver_on_a_separate_file(
    tmp_path: Path,
) -> None:
    """存档是独立 SQLite 文件（业务库表集合不变），连接随生命周期开、关，不留悬挂连接。"""
    data_dir = tmp_path / "data"
    app = create_app(data_dir)

    async with _serving(app):
        saver = app.state.checkpointer
        assert isinstance(saver, AsyncSqliteSaver)
        assert app.state.db.is_open

        checkpoint_path = checkpoint_database_path(data_dir)
        assert checkpoint_path.name == CHECKPOINT_DATABASE_FILENAME
        assert checkpoint_path != database_path(data_dir)
        assert checkpoint_path.is_file()

    await _assert_saver_closed(saver)
    assert not app.state.db.is_open
    # 业务库不因接入 checkpointer 多出 checkpoint 表（表集合断言见 test_langgraph_stage0.py）。
    assert not _checkpoint_tables(database_path(data_dir)) & {"checkpoints", "writes"}


async def test_lifespan_cancellation_closes_the_checkpoint_connection(
    tmp_path: Path,
) -> None:
    """运行期取消退出：finally 仍关闭存档连接，取消不被吞并照常传播。"""
    app = create_app(tmp_path / "data")
    serving = asyncio.Event()

    async def serve() -> None:
        async with app.router.lifespan_context(app):
            serving.set()
            await asyncio.Event().wait()  # 模拟服务进行中，直到被取消

    task = asyncio.create_task(serve())
    await serving.wait()
    saver = app.state.checkpointer
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_saver_closed(saver)
    assert not app.state.db.is_open


async def test_checkpointer_path_is_configured_independently_of_the_business_database(
    tmp_path: Path,
) -> None:
    """存档路径由独立入口给出：可指向业务库以外的任意路径，父目录自动创建，退出即关闭。"""
    configured = tmp_path / "configured" / "checkpoints" / "stage3.db"

    async with open_checkpointer(configured) as saver:
        graph = _pausing_graph(saver)
        await graph.ainvoke(
            _initial_state(CONVERSATION_ID, 1), thread_config(CONVERSATION_ID)
        )
        snapshot = await graph.aget_state(thread_config(CONVERSATION_ID))
        assert snapshot.next == (WAITING_NODE,)

    assert configured.is_file()
    await _assert_saver_closed(saver)
    # checkpoint 表由 saver 自己在独立文件里管理：该文件里没有业务表。
    assert _checkpoint_tables(configured) == {"checkpoints", "writes"}


# ---------- 真实 interrupt、重启恢复与线程隔离 ----------


async def test_waiting_state_survives_restart_with_fixture_draft_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 interrupt 落盘后关服务，重开（同一存档文件）仍按同一 thread 定位等待确认与 fixture draft；
    全程不调模型、不需要 API Key，且不激活／不归档计划、不改动原 active 计划。"""
    secret = "stage3-checkpoint-secret-must-not-persist"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    data_dir = tmp_path / "data"

    first = create_app(data_dir)
    async with _serving(first):
        db = first.state.db
        active_plan_id = await _insert_plan(db, version=1, status="active")
        draft_plan_id = await _insert_plan(db, version=2, status="draft")

        graph = _pausing_graph(first.state.checkpointer)
        result = await graph.ainvoke(
            _initial_state(CONVERSATION_ID, draft_plan_id),
            thread_config(CONVERSATION_ID),
        )
        assert result["__interrupt__"]  # 真暂停，不是节点内伪造的返回值

        snapshot = await graph.aget_state(thread_config(CONVERSATION_ID))
        assert snapshot.next == (WAITING_NODE,)
        assert snapshot.values["conversation_id"] == CONVERSATION_ID
        assert snapshot.values["draft_plan_id"] == draft_plan_id
        assert snapshot.values["confirmation"] == "pending"
        # Checkpoint 只装冻结的 State 字段：没有第二套线程身份，也没有密钥／Provider 字段。
        assert set(snapshot.values) <= set(WorkflowState.__annotations__)

        assert await _plan_facts(db, draft_plan_id) == ("draft", None)
        assert await _active_plan_ids(db) == (active_plan_id,)

    checkpoint_path = checkpoint_database_path(data_dir)
    assert secret.encode() not in checkpoint_path.read_bytes()

    # 重启：新 app 实例、新的 saver 连接，同一存档文件与同一 thread。
    second = create_app(data_dir)
    async with _serving(second):
        db = second.state.db
        recovered = _pausing_graph(second.state.checkpointer)
        snapshot = await recovered.aget_state(thread_config(CONVERSATION_ID))

        assert snapshot.next == (WAITING_NODE,)
        assert snapshot.values["draft_plan_id"] == draft_plan_id
        assert snapshot.values["confirmation"] == "pending"
        # 恢复只是读取：不激活、不归档，原 active 计划与 draft 事实都不变。
        assert await _plan_facts(db, draft_plan_id) == ("draft", None)
        assert await _plan_facts(db, active_plan_id) == ("active", None)
        assert await _active_plan_ids(db) == (active_plan_id,)


async def test_checkpointed_threads_do_not_share_state(tmp_path: Path) -> None:
    """不同 conversation 的等待状态各归各的 thread；未存档的 thread 不串到已有会话。"""
    data_dir = tmp_path / "data"
    app = create_app(data_dir)

    async with _serving(app):
        db = app.state.db
        first_draft_id = await _insert_plan(db, version=1, status="draft")
        # 第二条 fixture 计划只需要一个不同的身份：003 起 draft 受部分唯一索引限制（最多一条）。
        second_draft_id = await _insert_plan(db, version=2, status="rejected")
        graph = _pausing_graph(app.state.checkpointer)

        for conversation_id, draft_plan_id in (
            (CONVERSATION_ID, first_draft_id),
            (OTHER_CONVERSATION_ID, second_draft_id),
        ):
            await graph.ainvoke(
                _initial_state(conversation_id, draft_plan_id),
                thread_config(conversation_id),
            )

        first = await graph.aget_state(thread_config(CONVERSATION_ID))
        other = await graph.aget_state(thread_config(OTHER_CONVERSATION_ID))
        absent = await graph.aget_state(thread_config(ABSENT_CONVERSATION_ID))

        assert (first.values["conversation_id"], first.values["draft_plan_id"]) == (
            CONVERSATION_ID,
            first_draft_id,
        )
        assert (other.values["conversation_id"], other.values["draft_plan_id"]) == (
            OTHER_CONVERSATION_ID,
            second_draft_id,
        )
        assert first.next == other.next == (WAITING_NODE,)
        assert absent.next == ()
        assert dict(absent.values) == {}


# ---------- 固定节点与机密隔离 ----------


async def test_fixed_minimal_graph_runs_fixed_nodes_on_the_frozen_state(
    tmp_path: Path,
) -> None:
    """测试图只有固定节点（无模型节点、无 Planner／Evaluator 接线），且 State 就是冻结契约。"""
    async with open_checkpointer(tmp_path / "fixed.db") as saver:
        graph = _pausing_graph(saver)

        assert set(graph.get_graph().nodes) == {
            "__start__",
            "stage_draft",
            WAITING_NODE,
            "__end__",
        }
        assert set(WorkflowState.__annotations__) == {
            "conversation_id",
            "request",
            "intent",
            "context",
            "loaded_skill",
            "draft_plan_id",
            "draft_plan",
            "evaluation",
            "revision_count",
            "confirmation",
            "termination_reason",
        }


async def test_no_api_key_and_no_provider_config_reach_state_or_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 API Key 时非模型业务照常启动并落盘等待状态；Provider 配置的名称与取值都不进 State／存档。"""
    monkeypatch.delenv(MODEL_API_KEY_ENV, raising=False)
    base_url = "https://stage3-provider.invalid/v1"
    model_name = "stage3-model-must-not-persist"
    monkeypatch.setenv(MODEL_BASE_URL_ENV, base_url)
    monkeypatch.setenv(MODEL_MODEL_ENV, model_name)
    data_dir = tmp_path / "data"

    app = create_app(data_dir)
    async with _serving(app):
        db = app.state.db
        assert app.state.provider_has_api_key is False
        assert db.is_open
        draft_plan_id = await _insert_plan(db, version=1, status="draft")

        graph = _pausing_graph(app.state.checkpointer)
        await graph.ainvoke(
            _initial_state(CONVERSATION_ID, draft_plan_id),
            thread_config(CONVERSATION_ID),
        )
        snapshot = await graph.aget_state(thread_config(CONVERSATION_ID))

        assert snapshot.next == (WAITING_NODE,)
        assert snapshot.values["draft_plan_id"] == draft_plan_id
        # State 只有冻结契约的字段：provider 配置与密钥无处可放。
        assert set(snapshot.values) <= set(WorkflowState.__annotations__)

    serialized = checkpoint_database_path(data_dir).read_bytes()
    for forbidden in (
        MODEL_API_KEY_ENV,
        MODEL_BASE_URL_ENV,
        MODEL_MODEL_ENV,
        base_url,
        model_name,
    ):
        assert forbidden.encode() not in serialized
