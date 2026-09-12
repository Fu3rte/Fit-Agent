"""S4-02：Run 服务、幂等互斥、手动重试与重启恢复（stage4.md S4-02 验收）。

覆盖：
1. 相同 ``client_request_id`` 返回已有 Run，即使已有活跃 Run 也不报冲突（幂等先于 busy）。
2. 不同请求遇全局 ``pending``/``running`` → ``ConversationBusy``，不留 Run 或消息。
3. 并发两个不同请求只建立一个活跃 Run。
4. 终态后（completed/cancelled/failed）新请求可创建。
5. 手动重试创建带 ``retry_of_run_id`` 的新 Run，不复活旧记录，沿用原会话与原请求文本。
6. 旧 Run 仍活跃时重试被拒（不做断点续跑）。
7. 重启恢复：一个事务内把遗留 pending/running 标 failed 并各追加失败事件；终态不受影响；
   重复调用幂等；故障注入整体回滚。
8. 启动装配：迁移之后、对外服务之前执行恢复（进程内 lifespan）。
9. 普通执行失败：``running`` → ``failed`` 单事务写入原因与失败事件；pending 与全部终态拒绝；
   原因码限定为普通失败码（重启中断码只能由启动恢复写入）；中途失败整体回滚。

边界：本片创建 pending Run，并支持 ``running`` → ``failed`` 的普通失败流转；不启动模型调用、不接 SSE／取消／draining（S4-03 起）。
"""

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from api.app import create_app
from config import database_path
from runtime.error_codes import (
    INTERRUPTED_BY_RESTART,
    MODEL_REQUEST_TIMEOUT,
    RUN_TIMEOUT,
)
from runtime.run_service import RunService
from storage.errors import ConversationBusy, InvalidInput, NotFound, RunStateConflict
from storage.run_repo import RunRepo
from tests.support import open_database

_run_events_sql = "INSERT INTO run_events"


async def _counts(db) -> tuple[int, int]:
    """（Run 数，消息数）；仍经唯一锁访问，不在锁外直接操作连接。"""

    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM runs") as cursor:
            runs = int((await cursor.fetchone())[0])
        async with conn.execute("SELECT COUNT(*) FROM messages") as cursor:
            messages = int((await cursor.fetchone())[0])
        async with conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status = 'pending'"
        ) as cursor:
            pending = int((await cursor.fetchone())[0])
        return runs, messages, pending

    return await db.under_lock(op)


async def _event_count(db) -> int:
    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM run_events") as cursor:
            return int((await cursor.fetchone())[0])

    return await db.under_lock(op)


async def _service(db) -> RunService:
    return RunService(RunRepo(db))


async def _seed_legacy_run(
    db,
    *,
    run_id: str,
    status: str,
    conversation_id: str = "c1",
    event_type: str | None = None,
) -> None:
    """直接落一条“遗留”Run 行（模拟上一进程崩溃留下的库状态）。

    恢复路径只消费已有行，不经过创建路径；同一遗留库里可以同时存在多个 pending/running，
    这正是恢复必须覆盖全部行、而不是只处理“唯一活跃 Run”的原因。
    """
    ts = "2026-09-12T00:00:00+00:00"

    async with db.transaction() as conn:
        async with conn.execute(
            "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
        ) as cursor:
            if await cursor.fetchone() is None:
                await conn.execute(
                    "INSERT INTO conversations (id, created_at) VALUES (?, ?)",
                    (conversation_id, ts),
                )
        await conn.execute(
            "INSERT INTO runs (id, conversation_id, client_request_id, status, error_code,"
            " retry_of_run_id, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
            (run_id, conversation_id, f"cr-{run_id}", status, ts, ts),
        )
        if event_type is not None:
            await conn.execute(
                "INSERT INTO run_events (run_id, event_type, payload_json, created_at)"
                " VALUES (?, ?, '{}', ?)",
                (run_id, event_type, ts),
            )


# ---------- 验收 1–2：幂等先于 busy，冲突不留记录 ----------


async def test_same_client_request_returns_existing_run_even_while_active(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        await RunRepo(db).create_conversation("c1")
        first = await service.submit_request(
            conversation_id="c1",
            run_id="r1",
            client_request_id="cr-1",
            text="帮我安排训练",
        )
        assert first["created"] is True and first["run"]["status"] == "pending"

        again = await service.submit_request(
            conversation_id="c1",
            run_id="r2",
            client_request_id="cr-1",
            text="帮我安排训练",
        )
        assert again["created"] is False
        assert again["run"]["id"] == "r1"
        assert await _counts(db) == (1, 1, 1)


async def test_different_request_while_active_is_busy_without_writing(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        await RunRepo(db).create_conversation("c1")
        await service.submit_request(
            conversation_id="c1",
            run_id="r1",
            client_request_id="cr-1",
            text="第一个请求",
        )
        with pytest.raises(ConversationBusy, match="r1"):
            await service.submit_request(
                conversation_id="c1",
                run_id="r2",
                client_request_id="cr-2",
                text="第二个请求",
            )
        # 被拒请求不得留下 Run 或消息
        assert await _counts(db) == (1, 1, 1)


async def test_concurrent_different_requests_create_single_active_run(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        await RunRepo(db).create_conversation("c1")
        results = await asyncio.gather(
            service.submit_request(
                conversation_id="c1",
                run_id="rA",
                client_request_id="cr-a",
                text="请求 A",
            ),
            service.submit_request(
                conversation_id="c1",
                run_id="rB",
                client_request_id="cr-b",
                text="请求 B",
            ),
            return_exceptions=True,
        )
        created = [
            r for r in results if not isinstance(r, BaseException) and r["created"]
        ]
        busy = [r for r in results if isinstance(r, ConversationBusy)]
        assert len(created) == 1 and len(busy) == 1
        assert await _counts(db) == (1, 1, 1)


async def test_new_request_allowed_after_terminal_states(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await service.submit_request(
            conversation_id="c1", run_id="r1", client_request_id="cr-1", text="请求一"
        )
        await repo.cancel_run("r1")
        second = await service.submit_request(
            conversation_id="c1", run_id="r2", client_request_id="cr-2", text="请求二"
        )
        assert second["created"] is True
        await repo.start_run("r2")
        await repo.fail_interrupted_runs(INTERRUPTED_BY_RESTART)
        third = await service.submit_request(
            conversation_id="c1", run_id="r3", client_request_id="cr-3", text="请求三"
        )
        assert third["created"] is True
        assert (await repo_get(db, "r3"))["status"] == "pending"


# ---------- 验收 5–6：手动重试 ----------


async def test_manual_retry_creates_linked_run_and_keeps_old_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        service = await _service(db)
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await service.submit_request(
            conversation_id="c1",
            run_id="r-old",
            client_request_id="cr-old",
            text="帮我记录深蹲",
        )
        await repo.start_run("r-old")
        await repo.fail_interrupted_runs(INTERRUPTED_BY_RESTART)

        retried = await service.retry_request(
            previous_run_id="r-old", run_id="r-new", client_request_id="cr-new"
        )
        assert retried["created"] is True
        new_run = retried["run"]
        assert new_run["retry_of_run_id"] == "r-old"
        assert new_run["status"] == "pending"
        # 同一会话、同一请求文本重建本 Run 上下文；旧 Run 不被复活
        messages = await repo.list_run_messages("r-new")
        assert [m["kind"] for m in messages] == ["user_request"]
        assert await repo.get_user_request_text("r-new") == "帮我记录深蹲"
        old = await repo.get_run("r-old")
        assert old is not None and old["status"] == "failed"

    async with open_database(path) as db:
        assert (await repo_get(db, "r-new"))["retry_of_run_id"] == "r-old"


async def repo_get(db, run_id: str) -> dict:
    """按身份读取 Run 并断言存在（避免在断言里对 Optional 下标）。"""
    run = await RunRepo(db).get_run(run_id)
    assert run is not None, f"Run 不存在: {run_id}"
    return run


async def test_manual_retry_is_idempotent_and_requires_existing_previous_run(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await service.submit_request(
            conversation_id="c1", run_id="r1", client_request_id="cr-1", text="请求"
        )
        await repo.cancel_run("r1")

        first = await service.retry_request(
            previous_run_id="r1", run_id="r2a", client_request_id="cr-retry"
        )
        again = await service.retry_request(
            previous_run_id="r1", run_id="r2b", client_request_id="cr-retry"
        )
        assert first["created"] is True and again["created"] is False
        assert again["run"]["id"] == str(first["run"]["id"])
        assert (await _counts(db))[0] == 2

        with pytest.raises(NotFound, match="不存在"):
            await service.retry_request(
                previous_run_id="missing", run_id="r3", client_request_id="cr-3"
            )


async def test_manual_retry_while_previous_still_active_is_busy(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await service.submit_request(
            conversation_id="c1", run_id="r1", client_request_id="cr-1", text="请求"
        )
        await repo.start_run("r1")
        with pytest.raises(ConversationBusy, match="r1"):
            await service.retry_request(
                previous_run_id="r1", run_id="r2", client_request_id="cr-2"
            )
        assert (await _counts(db))[:2] == (1, 1)


# ---------- 验收 7：重启恢复 ----------


async def test_restart_recovery_fails_legacy_runs_with_event(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        service = await _service(db)
        repo = RunRepo(db)
        # 遗留库状态：崩溃前留下的行可以同时有多个 pending/running
        # （全局单 Run 只约束新创建；恢复必须覆盖全部遗留行）。
        await _seed_legacy_run(db, run_id="r-pending", status="pending")
        await _seed_legacy_run(
            db, run_id="r-running", status="running", conversation_id="c2"
        )
        await _seed_legacy_run(
            db,
            run_id="r-cancelled",
            status="cancelled",
            conversation_id="c4",
            event_type="cancelled",
        )
        await _seed_legacy_run(
            db,
            run_id="r-completed",
            status="completed",
            conversation_id="c3",
            event_type="failed",
        )

        failed = await service.recover_interrupted_runs()
        assert sorted(failed) == ["r-pending", "r-running"]

        for run_id in ("r-pending", "r-running"):
            run = await repo_get(db, run_id)
            assert run["status"] == "failed"
            assert run["error_code"] == INTERRUPTED_BY_RESTART
            events = await repo.list_run_events(run_id)
            assert [e["event_type"] for e in events] == ["failed"]
            assert INTERRUPTED_BY_RESTART in events[0]["payload_json"]

        # 终态 Run 不新增事件、状态不变
        completed = await repo_get(db, "r-completed")
        assert completed["status"] == "completed"
        assert completed["error_code"] is None
        # 终态 Run 的历史事件保持原样（不因恢复被改写或追加重试线索）
        assert [e["event_type"] for e in await repo.list_run_events("r-completed")] == [
            "failed"
        ]
        assert [e["event_type"] for e in await repo.list_run_events("r-cancelled")] == [
            "cancelled"
        ]

        # 幂等：再次恢复无遗留、且不追加任何事件
        before = await _event_count(db)
        assert await service.recover_interrupted_runs() == []
        assert await _event_count(db) == before

    # 关闭重开后仍在
    async with open_database(path) as db:
        run = await repo_get(db, "r-running")
        assert run["status"] == "failed" and run["error_code"] == INTERRUPTED_BY_RESTART


async def test_restart_recovery_rolls_back_all_runs_on_midway_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        await _seed_legacy_run(db, run_id="r1", status="pending")
        await _seed_legacy_run(db, run_id="r2", status="running", conversation_id="c2")

        original_execute = aiosqlite.Connection.execute

        def failing_execute(self, sql: str, *args, **kwargs):
            if _run_events_sql in sql:
                raise RuntimeError("boom: 失败事件步骤失败")
            return original_execute(self, sql, *args, **kwargs)

        monkeypatch.setattr(
            aiosqlite.Connection, "execute", failing_execute, raising=True
        )
        with pytest.raises(RuntimeError, match="boom"):
            await service.recover_interrupted_runs()
        monkeypatch.undo()

        # 任一步骤失败整体回滚：状态未变、无事件写入
        assert (await repo_get(db, "r1"))["status"] == "pending"
        assert (await repo_get(db, "r2"))["status"] == "running"
        assert await _event_count(db) == 0
        # 事务可继续使用：去掉故障注入后恢复成功
        assert sorted(await service.recover_interrupted_runs()) == ["r1", "r2"]
        assert (await repo_get(db, "r1"))["error_code"] == INTERRUPTED_BY_RESTART


# ---------- 验收 8：启动装配（迁移 → 恢复 → 对外服务） ----------


async def test_app_startup_recovers_legacy_runs_before_serving(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = database_path(data_dir)
    async with open_database(db_file) as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message(
            "c1", "r-legacy", "cr-legacy", "重启前的请求"
        )
        await repo.start_run("r-legacy")

    app = create_app(data_dir)
    async with app.router.lifespan_context(app):
        pass

    async with open_database(db_file, migrate=False) as db:
        run = await repo_get(db, "r-legacy")
        assert run["status"] == "failed"
        assert run["error_code"] == INTERRUPTED_BY_RESTART
        events = await RunRepo(db).list_run_events("r-legacy")
        assert [e["event_type"] for e in events] == ["failed"]


# ---------- 验收 9：普通执行失败 running→failed（P1 修复；不实现执行驱动） ----------


async def test_fail_run_marks_running_failed_with_event_and_persists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        service = await _service(db)
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await service.submit_request(
            conversation_id="c1", run_id="r1", client_request_id="cr-1", text="请求"
        )
        await repo.start_run("r1")

        failed = await service.fail_run(run_id="r1", error_code=MODEL_REQUEST_TIMEOUT)
        assert failed["status"] == "failed"
        assert failed["error_code"] == MODEL_REQUEST_TIMEOUT
        events = await repo.list_run_events("r1")
        assert [e["event_type"] for e in events] == ["failed"]
        assert MODEL_REQUEST_TIMEOUT in events[0]["payload_json"]
        # 失败不删用户请求，也不写任何 Assistant 消息／部分回答
        assert [m["kind"] for m in await repo.list_run_messages("r1")] == [
            "user_request"
        ]

    async with open_database(path) as db:
        run = await repo_get(db, "r1")
        assert run["status"] == "failed" and run["error_code"] == MODEL_REQUEST_TIMEOUT


async def test_fail_run_rejects_pending_and_all_terminal_states(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        repo = RunRepo(db)
        await repo.create_conversation("c1")

        # pending：普通失败不得从 pending 进入（pending→failed 只由启动恢复产生）
        await service.submit_request(
            conversation_id="c1", run_id="r-pending", client_request_id="cr-p", text="A"
        )
        with pytest.raises(RunStateConflict, match="仅 running"):
            await service.fail_run(run_id="r-pending", error_code=RUN_TIMEOUT)
        assert await _event_count(db) == 0

        # running → failed 成功后再调用：终态不可再流转
        await repo.start_run("r-pending")
        await service.fail_run(run_id="r-pending", error_code=RUN_TIMEOUT)
        with pytest.raises(RunStateConflict, match="仅 running"):
            await service.fail_run(run_id="r-pending", error_code=RUN_TIMEOUT)

        # completed / cancelled 两个终态同样拒绝，且不追加新的失败事件
        await _seed_legacy_run(
            db, run_id="r-completed", status="completed", conversation_id="c2"
        )
        await _seed_legacy_run(
            db, run_id="r-cancelled", status="cancelled", conversation_id="c3"
        )
        for run_id in ("r-completed", "r-cancelled"):
            with pytest.raises(RunStateConflict, match="仅 running"):
                await service.fail_run(run_id=run_id, error_code=RUN_TIMEOUT)
            assert await repo.list_run_events(run_id) == []

        # 不存在的 Run：明确报「不存在」，与「状态不允许」分开（不静默创建）
        with pytest.raises(NotFound, match="不存在"):
            await service.fail_run(run_id="missing", error_code=RUN_TIMEOUT)

        assert await _event_count(db) == 1  # 只有 r-pending 那一条失败事件


async def test_fail_run_rejects_non_ordinary_and_empty_error_codes(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await service.submit_request(
            conversation_id="c1", run_id="r1", client_request_id="cr-1", text="请求"
        )
        await repo.start_run("r1")

        # 重启中断码属启动恢复专用；未知码与空值同样拒绝
        for bad in (INTERRUPTED_BY_RESTART, "boom", ""):
            with pytest.raises(InvalidInput):
                await service.fail_run(run_id="r1", error_code=bad)

        run = await repo_get(db, "r1")
        assert run["status"] == "running" and run["error_code"] is None
        assert await _event_count(db) == 0


async def test_fail_run_rolls_back_status_when_event_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = await _service(db)
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await service.submit_request(
            conversation_id="c1", run_id="r1", client_request_id="cr-1", text="请求"
        )
        await repo.start_run("r1")

        original_execute = aiosqlite.Connection.execute

        def failing_execute(self, sql: str, *args, **kwargs):
            if _run_events_sql in sql:
                raise RuntimeError("boom: 失败事件步骤失败")
            return original_execute(self, sql, *args, **kwargs)

        monkeypatch.setattr(
            aiosqlite.Connection, "execute", failing_execute, raising=True
        )
        with pytest.raises(RuntimeError, match="boom"):
            await service.fail_run(run_id="r1", error_code=MODEL_REQUEST_TIMEOUT)
        monkeypatch.undo()

        # 状态与事件同事务：事件写不进去时状态不得变成 failed
        run = await repo_get(db, "r1")
        assert run["status"] == "running" and run["error_code"] is None
        assert await _event_count(db) == 0
        # 事务可继续使用
        assert (await service.fail_run(run_id="r1", error_code=RUN_TIMEOUT))[
            "status"
        ] == "failed"
