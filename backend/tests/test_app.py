"""S0-02：FastAPI 生命周期——唯一 aiosqlite 连接、重启复用原库、启动失败不服务。

生命周期经 ``app.router.lifespan_context`` 进程内驱动（不占用端口）；
数据目录全部使用 pytest tmp_path，不触碰真实用户数据目录（stage0.md 第 6 节）。
"""

import asyncio
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from api.app import create_app
from config import DATABASE_FILENAME
from storage.run_repo import RunRepo


async def test_lifespan_opens_connection_and_closes_on_shutdown(tmp_path: Path) -> None:
    """生命周期内保持唯一打开的连接；正常停服后不遗留本应用持有的连接。"""
    data_dir = tmp_path / "data"
    app = create_app(data_dir)
    async with app.router.lifespan_context(app):
        db = app.state.db
        assert db.is_open
        assert db.path == data_dir / DATABASE_FILENAME
        # 固定业务时区（S0-05）：生命周期内完成首次初始化，值为可解析的 IANA 地区名
        tz_name = app.state.business_timezone
        assert isinstance(tz_name, str) and tz_name
        ZoneInfo(tz_name)
        assert app.state.provider_has_api_key is False
    assert not db.is_open


async def test_lifespan_restart_reuses_database_with_sentinel(tmp_path: Path) -> None:
    """停服（close）后以同一数据目录重启：复用原库，哨兵记录保留。"""
    data_dir = tmp_path / "data"
    first = create_app(data_dir)
    async with first.router.lifespan_context(first):
        await RunRepo(first.state.db).create_conversation("sentinel")

    second = create_app(data_dir)
    async with second.router.lifespan_context(second):
        sentinel = await RunRepo(second.state.db).get_conversation("sentinel")
        assert sentinel is not None
        assert sentinel["id"] == "sentinel"


async def test_startup_failure_closes_connection_and_refuses_service(
    tmp_path: Path,
) -> None:
    """启动失败（数据库路径不可用）：异常上抛不对外服务，且不遗留已建立连接。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / DATABASE_FILENAME).mkdir()  # 数据库路径被目录占用：连接必然失败
    app = create_app(data_dir)
    with pytest.raises(sqlite3.OperationalError):
        async with app.router.lifespan_context(app):
            pass
    assert not app.state.db.is_open


async def test_lifespan_runtime_exception_closes_connection(tmp_path: Path) -> None:
    """运行阶段 lifespan 主体异常退出：finally 仍关闭唯一连接（S0-02）。"""
    data_dir = tmp_path / "data"
    app = create_app(data_dir)
    with pytest.raises(RuntimeError, match="boom"):
        async with app.router.lifespan_context(app):
            assert app.state.db.is_open
            raise RuntimeError("boom")
    assert not app.state.db.is_open


async def test_lifespan_cancellation_closes_connection(tmp_path: Path) -> None:
    """运行阶段取消退出：finally 仍关闭唯一连接，取消不被吞并照常传播。"""
    data_dir = tmp_path / "data"
    app = create_app(data_dir)
    serving = asyncio.Event()

    async def serve() -> None:
        async with app.router.lifespan_context(app):
            serving.set()
            await asyncio.Event().wait()  # 模拟服务进行中，直到被取消

    task = asyncio.create_task(serve())
    await serving.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not app.state.db.is_open
