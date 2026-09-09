"""S0-02/S0-03：连接生命周期、PRAGMA、唯一锁与事务入口。

确定性交错全部用 asyncio.Event 控制，不靠 sleep 猜时序。
"""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from storage.db import Database
from storage.migrations import load_migrations
from storage.run_repo import RunRepo
from tests.support import open_database

LATEST_VERSION = len(load_migrations())


async def test_open_initializes_wal_foreign_keys_busy_timeout(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        assert await db.pragma_value("journal_mode") == "wal"
        assert await db.pragma_value("foreign_keys") == 1
        assert await db.pragma_value("busy_timeout") == 5000


async def test_pragma_query_is_allowlisted(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        with pytest.raises(ValueError, match="不允许查询"):
            await db.pragma_value("database_list")


async def test_close_then_reopen_keeps_sentinel(tmp_path: Path) -> None:
    """停服（close）后重开同一文件库：哨兵记录保留；连接不留死锁状态。"""
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        repo = RunRepo(db)
        await repo.create_conversation("sentinel")
    async with open_database(path) as db:
        sentinel = await RunRepo(db).get_conversation("sentinel")
        assert sentinel is not None
        assert sentinel["id"] == "sentinel"


async def test_close_waits_for_underlying_close_when_cancelled(tmp_path: Path) -> None:
    """close 等底层关闭期间被反复取消：取消只记账，真实关闭跑完才上抛（S0-02）。

    两次取消都确定地落在等待窗口内，不用 sleep 猜时序：

    1. ``close_started`` 置位时 slow_close 正在首步里跑，调用方必然停在
       “等底层 close settle”的等待点上 —— 此刻的 cancel() 是取消 #1；
    2. 被取消 #1 唤醒后调用方再次挂在等待点上，底层关闭协程（它自身从不被
       取消，被取消的是调用方）在真正关库前发取消 #2 —— 落在“等 settle”期间。

    若取消 #2 能提前上抛，底层关闭会被丢在后台继续跑；测试 loop 随后关闭时，
    aiosqlite 工作线程的 call_soon_threadsafe 会撞上已关闭的 loop（线程异常告警）。
    close_finished 只在真实关库之后置位，因此它是“上抛前底层确实 settle 完”的证据。
    """
    db = Database(tmp_path / "app.db")
    await db.open()
    assert db._conn is not None
    real_close = db._conn.close
    close_started = asyncio.Event()
    release = asyncio.Event()
    close_finished = asyncio.Event()

    async def slow_close() -> None:
        # 人为拉长底层关闭，制造“close 等底层 settle 期间被取消”的确定性窗口
        close_started.set()
        await release.wait()
        task.cancel()  # 取消 #2：落在调用方等待底层关闭 settle 的期间
        await real_close()
        close_finished.set()

    db._conn.close = slow_close  # type: ignore[method-assign]

    task = asyncio.create_task(db.close())
    await close_started.wait()
    task.cancel()  # 取消 #1
    release.set()  # 释放底层关闭，使其能 settle
    with pytest.raises(asyncio.CancelledError):
        await task
    # 上抛发生在底层关闭 settle 之后：不半途遗弃，也不把工作线程留给已关闭的 loop
    assert close_finished.is_set()
    assert not db.is_open
    await db.open()
    assert db.is_open
    await db.close()
    assert not db.is_open


async def test_transaction_commit_and_rollback(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO conversations (id, created_at) VALUES ('c-ok', 't')"
            )
        assert await db.pragma_value("user_version") == LATEST_VERSION
        assert (await RunRepo(db).get_conversation("c-ok")) is not None

        with pytest.raises(RuntimeError, match="boom"):
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO conversations (id, created_at) VALUES ('c-bad', 't')"
                )
                raise RuntimeError("boom")
        # 异常中途不留下部分写入，事务已关闭
        assert (await RunRepo(db).get_conversation("c-bad")) is None
        # 直接读私有连接属性验证事务状态（测试边界，生产代码不这样做）
        conn = db._conn
        assert conn is not None
        assert not conn.in_transaction


async def test_concurrent_read_never_sees_uncommitted_state(tmp_path: Path) -> None:
    """写入事务未提交时，并发读取被唯一锁串行化：既看不到未提交行，也不打断事务。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        order: list[str] = []
        writer_started = asyncio.Event()
        gate_commit = asyncio.Event()

        async def writer() -> None:
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO conversations (id, created_at) VALUES ('c2', 't')"
                )
                order.append("writer_write")
                writer_started.set()
                await gate_commit.wait()
            order.append("writer_commit")

        results: list[dict[str, object] | None] = []

        async def reader() -> None:
            await writer_started.wait()
            results.append(await repo.get_conversation("c2"))
            order.append("reader_read")

        writer_task = asyncio.create_task(writer())
        reader_task = asyncio.create_task(reader())
        await writer_started.wait()
        gate_commit.set()
        await asyncio.gather(writer_task, reader_task)

        # 读取只能发生在提交之后（锁串行化），因此读到的是已提交行；
        # 若锁失效，reader_read 会插到 writer_commit 之前（共享连接脏读）。
        assert order == ["writer_write", "writer_commit", "reader_read"]
        committed = results[0]
        assert committed is not None
        assert committed["id"] == "c2"


async def test_cancel_inside_transaction_rolls_back_and_releases_lock(
    tmp_path: Path,
) -> None:
    """协程取消：不留开放事务、不留被占用的锁、无部分写入，后续读写仍能成功。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        started = asyncio.Event()
        gate = asyncio.Event()

        async def victim() -> None:
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO conversations (id, created_at) VALUES ('cx', 't')"
                )
                started.set()
                await gate.wait()

        task = asyncio.create_task(victim())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 取消触发回滚：无部分写入，也没有遗留的开放事务
        assert (await repo.get_conversation("cx")) is None
        # 直接读私有连接属性验证事务状态（测试边界，生产代码不这样做）
        conn = db._conn
        assert conn is not None
        assert not conn.in_transaction
        # 锁已释放：后续读写仍能成功
        await repo.create_conversation("cy")
        assert (await repo.get_conversation("cy")) is not None


async def test_cancel_during_begin_await_rolls_back_after_begin_settles(
    tmp_path: Path,
) -> None:
    """BEGIN 等待窗口内被取消：持锁等 BEGIN settle → 回滚 → 才传播取消（S0-03）。

    阻塞 UDF 先占住 aiosqlite 后台工作线程，使已排队的 BEGIN 必定尚未执行；
    取消发出后才释放线程，后台线程这时才真正开启事务——正是“await BEGIN 后
    同步检查 in_transaction”会漏掉的竞态窗口。
    """
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        conn = db._conn
        assert conn is not None

        entered_blocker = threading.Event()
        release_blocker = threading.Event()

        def blocker() -> int:
            entered_blocker.set()
            release_blocker.wait()
            return 1

        await conn.create_function("blocker", 0, blocker)

        begin_submitted = asyncio.Event()
        real_execute = conn.execute

        def execute_noting_begin(sql: str, *args: Any, **kwargs: Any) -> Any:
            # 同步包装，保留底层 await / async with 两种用法。
            if sql == "BEGIN":
                # BEGIN 已提交给独立 Task：此处到 shield await 之间无挂起点，
                # 因此随后的取消必定落在 BEGIN await 窗口内。
                begin_submitted.set()
            return real_execute(sql, *args, **kwargs)

        conn.execute = execute_noting_begin  # type: ignore[method-assign]

        async def victim() -> None:
            async with db.transaction():
                raise AssertionError("BEGIN 未 settle 前不应进入事务体")

        # 占住工作线程：此后排入队列的语句（含 BEGIN）都只能等线程空闲。
        blocker_task = asyncio.create_task(conn.execute("SELECT blocker()"))
        await asyncio.to_thread(entered_blocker.wait)
        try:
            task = asyncio.create_task(victim())
            await begin_submitted.wait()
            task.cancel()
            release_blocker.set()

            with pytest.raises(asyncio.CancelledError):
                await task
            await blocker_task

            # 取消等到 BEGIN 真正执行完才传播：残留事务已被回滚，不留开放事务。
            assert not conn.in_transaction
            # 锁已释放且连接干净：后续事务仍能成功。
            async with db.transaction() as tx_conn:
                await tx_conn.execute(
                    "INSERT INTO conversations (id, created_at) VALUES ('c-after', 't')"
                )
            assert (await repo.get_conversation("c-after")) is not None
        finally:
            release_blocker.set()


async def test_single_statement_write_waits_for_open_transaction(
    tmp_path: Path,
) -> None:
    """单语句写入同样经唯一锁：开启中的事务提交前，写入被串行化在提交之后。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        order: list[str] = []
        writer_started = asyncio.Event()
        gate_commit = asyncio.Event()

        async def writer() -> None:
            async with db.transaction() as conn:
                await conn.execute(
                    "INSERT INTO conversations (id, created_at) VALUES ('c2', 't')"
                )
                order.append("writer_write")
                writer_started.set()
                await gate_commit.wait()
            order.append("writer_commit")

        async def single_statement_writer() -> None:
            await writer_started.wait()
            await repo.create_conversation("c3")
            order.append("single_write")

        writer_task = asyncio.create_task(writer())
        single_task = asyncio.create_task(single_statement_writer())
        await writer_started.wait()
        gate_commit.set()
        await asyncio.gather(writer_task, single_task)

        assert order == ["writer_write", "writer_commit", "single_write"]
        assert (await repo.get_conversation("c2")) is not None
        assert (await repo.get_conversation("c3")) is not None


async def test_connection_is_not_exposed_publicly(tmp_path: Path) -> None:
    """无绕开锁的生产数据库访问路径：连接对象不进入公开 API 表面。"""
    async with open_database(tmp_path / "app.db") as db:
        public = {name for name in dir(db) if not name.startswith("_")}
        assert "conn" not in public
        assert "connection" not in public


async def test_operations_before_open_fail(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    with pytest.raises(RuntimeError, match="未打开"):
        await db.pragma_value("journal_mode")
    await db.close()  # 未打开时 close 是安全空操作
