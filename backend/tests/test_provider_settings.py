"""S0-07：Provider 配置与密钥存储边界（10.3、07 7.4「完整 API Key 不得进入 run_events 或异常详情」）。

覆盖 stage0.md S0-07 验收标准：
1. 录入 / 替换 / 显式删除结果跨临时文件库重开保持。
2. 默认查询只返回 has_api_key，不返回完整 Key，首版底座不主动增加掩码字段。
3. 内部取 Key 路径与公开查询投影隔离（只有 get_provider_api_key_internal 返回 Key）。
4. 运行时生成的辨识度明确的假 Key 不进入捕获的响应投影、日志、Trace 替身、
   run_events、messages 或其他表，也不进入校验/存储失败路径的异常详情；
   仅允许存在于批准的 Provider 存储位置（provider_config.api_key）。
5. 正常与失败路径都留下实际运行证据（SQLite trace 回调、日志捕获、全表逐格扫描、
   真实入口 /healthz 响应与 uvicorn 服务日志），不只做源码字符串扫描。
   日志断言覆盖 **全部 logger（含第三方驱动如 aiosqlite）**，root 被主动拉到 DEBUG：
   任何 logging record 都不得含完整 Key（10.3 绝对边界；凭据写入期间由
   Database.parameter_echo_suppressed 压制驱动的参数回显 DEBUG 日志，退出后恢复原状态）。
   sqlite3 set_trace_callback 会展开绑定值且未接入生产路径：保留为明确的禁止接线
   风险证据，并留给 Stage 4 约束。

边界（stage0.md S0-07 依赖与第 4 节）：不接设置页、不新增 HTTP 端点、不做真实
模型调用或连通性探测；未接入的模型请求 Trace 链路由 Stage 4 复验。
全部使用 pytest tmp_path 下的独立临时文件库与假 Key，不读取真实 API Key、不联网。
"""

import asyncio
import logging
import os
import secrets
import sqlite3
import threading
import traceback
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import aiosqlite
import pytest

from config import DATABASE_FILENAME
from storage.db import Database
from storage.errors import InvalidInput, NotFound, RunStateConflict
from storage.run_repo import RunRepo
from storage.setting_repo import DEFAULT_PROVIDER, SettingRepo
from tests.support import (
    BACKEND_ROOT,
    dump_framework_message,
    free_port,
    open_database,
    start_app,
    stop_app,
    text_response,
    wait_for_healthz,
)

# ---------- 假 Key 与泄漏检查助手 ----------


_SCANNED_TABLES = frozenset(
    {
        "conversations",
        "runs",
        "messages",
        "run_events",
        "app_config",
        "provider_config",
        # Stage 1 新增的两项业务存储：同样必须被凭据泄漏扫描覆盖
        "exercises",
        "user_profile",
        # Stage 2 新增的草稿表（S2-02）：同样必须被扫描覆盖
        "business_drafts",
        # Stage 3 新增的计划侧三表（S3-02）：同样必须被扫描覆盖
        "plan_versions",
        "scheduled_sessions",
        "arrangement_revisions",
        # Stage 3 新增的记录侧四表（S3-09）：同样必须被扫描覆盖
        "training_sessions",
        "session_revisions",
        "exercise_logs",
        "training_sets",
        # Stage 3 新增的复盘两表（S3-13）：同样必须被扫描覆盖
        "reviews",
        "review_source_revisions",
    }
)


def fake_api_key(tag: str) -> str:
    """运行时生成辨识度明确的假 Key：固定假凭据前缀 + 用例标记 + 随机段。

    每次调用都不同，绝不来自真实凭据、环境变量或 .env（stage0.md 第 6 节）。
    """
    return f"sk-fitagent-fake-{tag}-{secrets.token_hex(16)}"


def flatten(value: Any) -> str:
    """把嵌套结构（dict/list/tuple/标量）展平成可扫描文本，用于泄漏断言。"""
    if isinstance(value, dict):
        return " ".join(f"{flatten(k)}={flatten(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(flatten(item) for item in value)
    return repr(value)


def assert_no_key(value: Any, key: str, where: str) -> None:
    assert key not in flatten(value), f"假 Key 进入 {where}"


def exception_evidence(raised: pytest.ExceptionInfo[BaseException]) -> str:
    """异常对象 + 完整 traceback 文本 + __cause__ 链的合并证据。"""
    parts: list[str] = [repr(raised.value), str(raised.value)]
    parts.extend(traceback.format_exception(raised.value))
    cause: BaseException | None = raised.value.__cause__
    while cause is not None:
        parts.extend([repr(cause), str(cause), *traceback.format_exception(cause)])
        cause = cause.__cause__
    return "\n".join(parts)


# Stage 0 底座建立的全部表（S0-04 迁移 001）；逐表字面量查询，表名不走变量。


def _rows_as_cells(
    table: str, columns: tuple[str, ...], rows: Iterable[Any]
) -> list[tuple[str, str, str]]:
    """把一张表的全部非空单元格展平成 (表名, 列名, 文本)。"""
    return [
        (table, column, str(cell))
        for row in rows
        for column, cell in zip(columns, row, strict=True)
        if cell is not None
    ]


async def all_text_cells(db: Database) -> list[tuple[str, str, str]]:
    """逐表逐格读取库中全部文本单元格（仍经唯一锁访问的测试专用读取）。

    返回 (表名, 列名, 文本) 列表，用于证明 Key 没有被复制进 messages、
    run_events 或其他任何表。先确认库内表集合与已知底座表集合一致（后续
    迁移新增表时本测试会大声失败并要求补进扫描清单），避免遗漏复制目标。
    """

    async def op(conn: aiosqlite.Connection) -> list[tuple[str, str, str]]:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ) as cursor:
            tables = [str(row[0]) for row in await cursor.fetchall()]
        assert {t for t in tables if not t.startswith("sqlite_")} == _SCANNED_TABLES, (
            f"库内表集合与扫描清单不一致，需补齐: {tables}"
        )

        cells: list[tuple[str, str, str]] = []
        async with conn.execute("SELECT * FROM conversations") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("conversations", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM runs") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("runs", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM messages") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("messages", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM run_events") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("run_events", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM app_config") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("app_config", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM provider_config") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("provider_config", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM exercises") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("exercises", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM user_profile") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("user_profile", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM business_drafts") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("business_drafts", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM plan_versions") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("plan_versions", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM scheduled_sessions") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells(
                "scheduled_sessions", columns, await cursor.fetchall()
            )
        async with conn.execute("SELECT * FROM arrangement_revisions") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells(
                "arrangement_revisions", columns, await cursor.fetchall()
            )
        async with conn.execute("SELECT * FROM training_sessions") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells(
                "training_sessions", columns, await cursor.fetchall()
            )
        async with conn.execute("SELECT * FROM session_revisions") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells(
                "session_revisions", columns, await cursor.fetchall()
            )
        async with conn.execute("SELECT * FROM exercise_logs") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("exercise_logs", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM training_sets") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("training_sets", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM reviews") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells("reviews", columns, await cursor.fetchall())
        async with conn.execute("SELECT * FROM review_source_revisions") as cursor:
            columns = tuple(str(c[0]) for c in cursor.description)
            cells += _rows_as_cells(
                "review_source_revisions", columns, await cursor.fetchall()
            )
        return cells

    return await db.under_lock(op)


def cells_with_key(
    cells: Iterable[tuple[str, str, str]], key: str
) -> set[tuple[str, str]]:
    return {(table, column) for table, column, text in cells if key in text}


# 日志捕获必须覆盖全部 logger：本项目自己的、uvicorn/asyncio 等框架的、以及
# 会把绑定参数（含明文 Key）渲染成日志的第三方驱动（aiosqlite DEBUG）。
# 不得白名单排除任何 logger、不得降低 caplog 捕获范围、不得依赖默认 INFO。


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """本次调用中所有 logger 实际打印出的日志文本（全量捕获，无排除）。"""
    return "\n".join(
        f"{record.name} {record.levelname} {record.getMessage()}"
        for record in caplog.get_records("call")
    )


def loggers_with_key(caplog: pytest.LogCaptureFixture, key: str) -> set[str]:
    """本次调用中真正把 Key 打印出来的 logger 名字集合（必须为空）。"""
    return {
        record.name
        for record in caplog.get_records("call")
        if key in record.getMessage()
    }


def assert_no_key_in_logs(caplog: pytest.LogCaptureFixture, key: str) -> None:
    """全量 logging records 零命中：不区分本项目与第三方驱动。"""
    offenders = loggers_with_key(caplog, key)
    assert offenders == set(), f"完整 Key 进入日志：{sorted(offenders)}"
    assert_no_key(all_log_text(caplog), key, "全部 logger 的日志记录")


# aiosqlite 参数回显 DEBUG 日志未接入时的默认状态（未显式设级、未禁用）。
DEFAULT_ECHOING_LOGGER = "aiosqlite"


def assert_echoing_logger_untouched(name: str = DEFAULT_ECHOING_LOGGER) -> None:
    """凭据写入退出后驱动 logger 必须回到原状态：不留临时压制、也不永久禁用。"""
    logger = logging.getLogger(name)
    assert logger.level == logging.NOTSET, (
        f"{name} 的临时压制没被恢复（level={logger.level}）"
    )
    assert logger.disabled is False, f"{name} 被遗留为禁用状态"


def key_copies_on_disk(data_dir: Path, key: str) -> dict[str, int]:
    """统计数据库目录里每个文件（含 WAL/SHM 残留）中假 Key 的字节出现次数。"""
    needle = key.encode()
    return {
        path.name: path.read_bytes().count(needle)
        for path in sorted(data_dir.iterdir())
        if path.is_file()
    }


class TraceRecorder:
    """Trace 替身：接口对齐 span 属性写入（真实 OTel/Logfire 链路归 Stage 4）。

    把存储层每个返回投影都当作 span 属性记录一次；若投影里带了 Key，替身就会拿到。
    """

    def __init__(self) -> None:
        self.records: list[str] = []

    def record(self, span: str, projection: Any) -> Any:
        self.records.append(f"{span} {flatten(projection)}")
        return projection

    def text(self) -> str:
        return "\n".join(self.records)


async def attach_sql_trace(db: Database) -> list[str]:
    """挂接 SQLite 语句 trace 回调，捕获实际执行的语句（仅测试侧，生产未接线）。

    注意：sqlite3 的语句 trace 回调拿到的是已展开绑定参数的完整语句（等价于
    sqlite3_expanded_sql），因此凭据写入语句在这个钩子上必然带出明文 Key——本
    函数因此同时是凭据风险探测点，结论与“生产未接线”实测见
    test_sqlite_statement_trace_expansion_is_not_wired_yet。
    """
    statements: list[str] = []

    async def op(conn: aiosqlite.Connection) -> None:
        await conn.set_trace_callback(statements.append)

    await db.under_lock(op)
    return statements


# ---------- 验收 1＋2＋3：增改删跨重开、默认投影、内部路径隔离 ----------


async def test_default_projection_returns_only_has_api_key(tmp_path: Path) -> None:
    """未配置时投影为 None；录入后默认投影只含 has_api_key，不含 Key、不含掩码。"""
    key = fake_api_key("project")
    async with open_database(tmp_path / "app.db") as db:
        repo = SettingRepo(db)
        assert await repo.get_provider_status(DEFAULT_PROVIDER) is None
        assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) is None

        written = await repo.set_provider_api_key(DEFAULT_PROVIDER, key)
        # 录入返回投影精确等于 provider + has_api_key：多字段（含掩码）即越界
        assert written == {"provider": DEFAULT_PROVIDER, "has_api_key": True}
        assert_no_key(written, key, "录入返回投影")

        status = await repo.get_provider_status(DEFAULT_PROVIDER)
        assert status is not None
        assert set(status) == {"provider", "has_api_key", "updated_at"}
        assert status["has_api_key"] is True
        assert_no_key(status, key, "默认查询投影")

        # 内部取 Key 路径：唯一允许返回 Key 的入口，与公开投影隔离
        assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) == key


async def test_replace_and_explicit_delete_survive_reopen(tmp_path: Path) -> None:
    """录入→替换→显式删除→再录入，每一步结果都跨同一文件库重开保持。"""
    path = tmp_path / "app.db"
    first = fake_api_key("replace-a")
    second = fake_api_key("replace-b")
    third = fake_api_key("replace-c")

    async with open_database(path) as db:
        await SettingRepo(db).set_provider_api_key(DEFAULT_PROVIDER, first)
    async with open_database(path) as db:  # 重开：录入保持
        repo = SettingRepo(db)
        assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) == first
        replaced = await repo.set_provider_api_key(DEFAULT_PROVIDER, second)
        assert replaced == {"provider": DEFAULT_PROVIDER, "has_api_key": True}
    async with open_database(path) as db:  # 重开：替换保持，旧 Key 不再是存储值
        repo = SettingRepo(db)
        assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) == second
        status = await repo.get_provider_status(DEFAULT_PROVIDER)
        assert status is not None and status["has_api_key"] is True
        assert await repo.delete_provider_api_key(DEFAULT_PROVIDER) == {
            "provider": DEFAULT_PROVIDER,
            "has_api_key": False,
        }
    async with open_database(path) as db:  # 重开：显式删除保持
        repo = SettingRepo(db)
        assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) is None
        status = await repo.get_provider_status(DEFAULT_PROVIDER)
        assert status is not None and status["has_api_key"] is False
        assert_no_key(status, second, "删除后的默认投影")
        # 删除后仍可重新录入（显式删除不是终态锁）
        await repo.set_provider_api_key(DEFAULT_PROVIDER, third)
    async with open_database(path) as db:
        assert (
            await SettingRepo(db).get_provider_api_key_internal(DEFAULT_PROVIDER)
            == third
        )


async def test_delete_and_query_are_scoped_per_provider(tmp_path: Path) -> None:
    """未配置的 provider 明确失败，不误清其他 provider，也不返回他人 Key。"""
    key = fake_api_key("scoped")
    async with open_database(tmp_path / "app.db") as db:
        repo = SettingRepo(db)
        await repo.set_provider_api_key("other-provider", key)
        with pytest.raises(NotFound):
            await repo.delete_provider_api_key(DEFAULT_PROVIDER)
        assert await repo.get_provider_status(DEFAULT_PROVIDER) is None
        assert await repo.get_provider_api_key_internal("other-provider") == key


# ---------- 验收 4：失败路径的异常详情不含 Key ----------


async def test_validation_failures_reject_without_leaking_key(tmp_path: Path) -> None:
    """空/纯空白/非字符串 Key、非法 provider：拒绝且异常详情不含 Key，原值不变。"""
    key = fake_api_key("validate")
    async with open_database(tmp_path / "app.db") as db:
        repo = SettingRepo(db)
        await repo.set_provider_api_key(DEFAULT_PROVIDER, key)

        for bad in ("", "   ", "\n\t", None, 12345, b"bytes"):
            with pytest.raises(InvalidInput) as raised:
                await repo.set_provider_api_key(DEFAULT_PROVIDER, cast(str, bad))
            assert_no_key(exception_evidence(raised), key, "Key 校验失败的异常详情")
            assert DEFAULT_PROVIDER in str(raised.value)  # 只带 provider 标识
            # 校验失败不落库、不清库
            assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) == key

        for bad_provider in ("", "  ", None, 7):
            with pytest.raises(InvalidInput) as raised:
                await repo.set_provider_api_key(
                    cast(str, bad_provider), fake_api_key("provider")
                )
            assert_no_key(
                exception_evidence(raised), key, "provider 校验失败的异常详情"
            )
            assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) == key

        with pytest.raises(NotFound) as raised:
            await repo.delete_provider_api_key("missing-provider")
        assert_no_key(exception_evidence(raised), key, "NotFound 异常详情")
        assert await repo.get_provider_status("missing-provider") is None


async def test_storage_failure_rolls_back_and_exception_has_no_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真实存储失败（参数不可绑定）：事务整体回滚，异常/traceback 与全量日志不带 Key。

    sqlite3 对不可绑定类型的实错是 ``ProgrammingError``（"Error binding parameter …:
    type 'object' is not supported"），该文案只报参数序号与类型，不回显参数值。
    失败路径同样在 DEBUG 压制窗口内（回滚语句也在窗口里），退出后状态恢复。
    """
    key = fake_api_key("storage-fail")
    replacement = fake_api_key("storage-fail-retry")
    caplog.set_level(logging.DEBUG)
    async with open_database(tmp_path / "app.db") as db:
        repo = SettingRepo(db)
        await repo.set_provider_api_key(DEFAULT_PROVIDER, key)
        assert_no_key_in_logs(caplog, key)

        monkeypatch.setattr("storage.setting_repo._now", lambda: object())
        with pytest.raises(sqlite3.ProgrammingError) as raised:
            await repo.set_provider_api_key(DEFAULT_PROVIDER, replacement)
        evidence = exception_evidence(raised)
        assert_no_key(evidence, key, "存储失败 traceback（旧 Key）")
        assert_no_key(evidence, replacement, "存储失败 traceback（新 Key）")
        # 失败路径的全量日志捕获（含驱动 DEBUG）也不得出现任何 Key
        assert_no_key_in_logs(caplog, key)
        assert_no_key_in_logs(caplog, replacement)
        assert_echoing_logger_untouched()
        monkeypatch.undo()

        # 失败不留下半套写入：旧 Key 仍在，库可继续正常使用并可完成替换
        status = await repo.get_provider_status(DEFAULT_PROVIDER)
        assert status is not None and status["has_api_key"] is True
        assert_no_key(status, replacement, "失败后的默认投影")
        assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) == key
        await repo.set_provider_api_key(DEFAULT_PROVIDER, replacement)
        assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) == replacement
        # 重试写入同样不留下 Key；失败后的库仍可正常走安全窗口
        assert_no_key_in_logs(caplog, replacement)
        assert_echoing_logger_untouched()


# ---------- 验收 4（主证据）：Key 只存在于批准的存储位置 ----------


async def test_key_never_copied_into_logs_trace_run_events_or_other_tables(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """录入后跑一遍运行时写入与失败路径：Key 只出现在 provider_config.api_key。

    捕获面：SQLite trace 回调（实际执行语句）、pytest 日志捕获、Trace 替身
    （span 属性）、run_events/messages/全部表逐格扫描、异常详情、库目录文件字节。

    root 被本用例主动拉到 DEBUG（高于生产默认）：全量捕获里任何 logger 都不得带出 Key，
    不靠排除驱动 logger、不靠降档捕获、不靠默认 INFO 过关。参数回显风险本身是真的，
    正控制与压制边界实测见
    test_bound_parameter_echo_is_suppressed_only_during_the_credential_write。
    """
    key = fake_api_key("leak")
    caplog.set_level(logging.DEBUG)
    logging.getLogger("fit_agent.s0_07").warning("正控制：日志捕获确实可用")

    async with open_database(tmp_path / "app.db") as db:
        statements = await attach_sql_trace(db)
        trace = TraceRecorder()
        settings = SettingRepo(db)
        runs = RunRepo(db)

        projection = trace.record(
            "provider.set", await settings.set_provider_api_key(DEFAULT_PROVIDER, key)
        )
        status = trace.record(
            "provider.status", await settings.get_provider_status(DEFAULT_PROVIDER)
        )
        assert await settings.get_provider_api_key_internal(DEFAULT_PROVIDER) == key

        # 运行时四表写入路径（S0-06 已拍操作）；事件负载直接取默认投影
        await runs.create_conversation("c1")
        await runs.create_run_with_user_message(
            "c1", "r1", "cr-1", "我刚录入了 API Key"
        )
        await runs.start_run("r1")
        await runs.save_partial_answer("r1", "正在检查 Provider 配置……")
        assert await runs.append_run_events(
            "r1",
            [
                ("provider_config_changed", {"projection": projection}),
                ("diagnostic", {"status": status}),
            ],
        )
        await runs.complete_run(
            "r1",
            [
                (
                    "assistant",
                    dump_framework_message(text_response("已保存 Provider 配置。")),
                )
            ],
        )
        trace.record("run.completed", await runs.get_run("r1"))
        trace.record("run.events", await runs.list_run_events("r1"))
        trace.record("run.messages", await runs.list_messages("c1"))
        trace.record(
            "config.timezone_init",
            await settings.initialize_business_timezone(lambda: "Asia/Shanghai"),
        )

        # 失败路径：不存在的 Run 取消必然失败，异常详情不带 Key
        # （S0-06 已拍契约：cancel_run 是条件更新，行不存在同样不命中 → RunStateConflict）
        with pytest.raises(RunStateConflict) as raised:
            await runs.cancel_run("r-none")
        assert_no_key(exception_evidence(raised), key, "取消失败的异常详情")

        cells = await all_text_cells(db)
        assert cells_with_key(cells, key) == {("provider_config", "api_key")}
        # trace 回调（仅本用例挂接，生产未接线）拿到的是展开绑定参数后的完整语句：
        # 逐条限定“带 Key 的只能是批准的凭据写入”，而不是排除本捕获面。
        provider_writes = [sql for sql in statements if "provider_config" in sql]
        assert provider_writes, "Provider 写入未被 trace 捕获"
        expanded_key_statements = [sql for sql in statements if key in sql]
        assert expanded_key_statements and set(expanded_key_statements) <= set(
            provider_writes
        ), "展开后的 SQLite 语句只能在凭据写入处带出 Key"
        # 除批准的凭据写入语句外，任何其他表（messages/run_events/app_config）的
        # 实际执行语句不得携带 Key
        assert_no_key(
            [sql for sql in statements if sql not in expanded_key_statements],
            key,
            "非凭据表的 SQLite 语句",
        )
        assert_no_key(trace.text(), key, "Trace 替身（span 属性）")

    # 全量日志捕获（所有 logger，含第三方驱动；root 已拉到 DEBUG）零命中。
    assert_no_key_in_logs(caplog, key)
    assert "正控制：日志捕获确实可用" in caplog.text
    # 临时压制不得泄漏成全局禁用状态。
    assert_echoing_logger_untouched()
    copies = key_copies_on_disk(tmp_path, key)
    assert copies == {DATABASE_FILENAME: 1}, f"Key 在磁盘上出现多余副本: {copies}"


async def test_internal_key_value_is_not_written_back_anywhere(tmp_path: Path) -> None:
    """内部取到 Key 后走一轮事件/消息写入（只传投影），Key 不随之落库到其他表。"""
    key = fake_api_key("roundtrip")
    async with open_database(tmp_path / "app.db") as db:
        settings = SettingRepo(db)
        runs = RunRepo(db)
        await settings.set_provider_api_key(DEFAULT_PROVIDER, key)
        secret = await settings.get_provider_api_key_internal(DEFAULT_PROVIDER)
        assert secret == key

        await runs.create_conversation("c1")
        await runs.create_run_with_user_message(
            "c1", "r1", "cr-1", "测试 Provider 配置"
        )
        await runs.start_run("r1")
        await runs.append_run_events(
            "r1",
            [
                (
                    "provider_status",
                    await settings.get_provider_status(DEFAULT_PROVIDER) or {},
                )
            ],
        )
        cells = await all_text_cells(db)
        assert cells_with_key(cells, key) == {("provider_config", "api_key")}


# ---------- 验收 4（真实入口响应投影）：/healthz 与 uvicorn 日志 ----------


async def test_real_entrypoint_response_and_server_logs_have_no_key(
    tmp_path: Path,
) -> None:
    """已接线的唯一响应投影 /healthz：只暴露布尔；响应与服务日志均无 Key。

    服务日志覆盖生产默认 INFO（uvicorn 默认日志级别，与 main.py 一致）与最坏情况
    ``--log-level debug``（两个级别都会启动真实子进程并拿到非空服务日志输出）。
    不用 pytest.mark.parametrize：anyio 插件为 async 测试重建 callspec 时会丢弃
    既有参数化（request.param 缺失，见 tests/test_timezone.py 同一说明），故在循环内逐级别跑。
    设置页与凭据 HTTP 端点未实现（stage0.md 第 4 节），本用例不声称端到端凭据验收。
    """
    for log_level in ("info", "debug"):
        await _check_real_entrypoint_has_no_key(
            tmp_path / f"data-{log_level}", log_level
        )


async def _check_real_entrypoint_has_no_key(data_dir: Path, log_level: str) -> None:
    """以真实入口启动一次服务，检查响应投影与服务日志，并回收子进程。"""
    key = fake_api_key(f"http-{log_level}")
    data_dir.mkdir()
    async with open_database(data_dir / DATABASE_FILENAME) as db:
        await SettingRepo(db).set_provider_api_key(DEFAULT_PROVIDER, key)

    port = free_port()
    process = start_app(data_dir, port, log_level=log_level)
    try:
        http_status, body = wait_for_healthz(port)
        assert http_status == 200
        assert '"provider_has_api_key":true' in body.replace(" ", "")
        assert_no_key(body, key, "/healthz 响应")
    finally:
        server_log = stop_app(process)

    assert server_log != "", "未捕获到服务日志输出"
    assert_no_key(server_log, key, f"uvicorn 服务日志（--log-level {log_level}）")
    # 服务重启不改写凭据位置：Key 仍只在批准的存储位置
    assert sum(key_copies_on_disk(data_dir, key).values()) == 1


# 生产代码范围（不扫 tests/、归档与依赖环境）：S0-07 底座实际接入路径。
_PRODUCTION_PACKAGES = ("api", "app", "domain", "runtime", "storage")
_PRODUCTION_FILES = ("config.py", "main.py")
# 会把绑定参数（含明文 Key）渲染成文本的诊断入口。
_PARAMETER_ECHOING_HOOKS = ("set_trace_callback", "set_authorizer_callback")
_DEBUG_LOGGING_WIRES = (
    "basicConfig",
    "dictConfig",
    "fileConfig",
    'getLogger("aiosqlite")',
)


def _production_sources() -> list[Path]:
    sources: list[Path] = []
    for package in _PRODUCTION_PACKAGES:
        sources += sorted((BACKEND_ROOT / package).rglob("*.py"))
    sources += [BACKEND_ROOT / name for name in _PRODUCTION_FILES]
    return sources


async def test_sqlite_statement_trace_expansion_is_not_wired_yet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """已确认边界：会展开绑定参数的诊断钩子尚未接入生产路径（不是断言它们安全）。

    三条证据，不用源码扫描替代运行实测：
    1. 正控制：sqlite3 trace 回调确实拿到展开后的完整语句，会带出明文 Key
       （所以本用例把它当风险实测，而不是删掉捕获）。
    2. 生产代码（storage/api/app/domain/runtime/config.py/main.py）没挂 trace 回调，
       也没有任何日志配置会把第三方参数回显调试日志拉起来。
    3. 真进程服务日志（INFO 默认与 ``--log-level debug``）无 Key：见
       test_real_entrypoint_response_and_server_logs_have_no_key（生产启动不配置
       日志也不拉高任何 logger 级别的等价证据）。

    aiosqlite 的参数回显 DEBUG 日志是普通 logging 面，不属于本用例的“未接线”边界：
    凭据写入已必须由 Database.parameter_echo_suppressed 包住，实测见
    test_bound_parameter_echo_is_suppressed_only_during_the_credential_write。

    留给 Stage 4 的硬约束：接 OTel/Logfire 或 SQL trace 时必须排除/掩码凭据写入，
    否则 10.3「完整 Key 不得进入日志、Trace」会被参数展开直接破坏。本阶段不预先
    接入也不预先声称已验收。
    """
    # 1) 正控制：trace 回调确实会展开绑定参数。
    expanded_key = fake_api_key("trace-expanded")
    caplog.set_level(logging.DEBUG)
    async with open_database(tmp_path / "app.db") as db:
        statements = await attach_sql_trace(db)
        await SettingRepo(db).set_provider_api_key(DEFAULT_PROVIDER, expanded_key)
        traced = [sql for sql in statements if expanded_key in sql]
        assert traced, "sqlite3 trace 回调本应展开绑定参数却没展开"
        assert any("provider_config" in sql for sql in traced)
    # 同一次运行里全量 logging records 依旧零命中（含 aiosqlite）。
    assert_no_key_in_logs(caplog, expanded_key)
    assert_echoing_logger_untouched()

    # 2) 生产代码未接入这两个入口（仅作为上述运行证据的补充，不单独成凭）。
    sources = _production_sources()
    assert len(sources) >= 6, f"生产源码扫描范围异常缩小：{len(sources)} 个文件"
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for hook in _PARAMETER_ECHOING_HOOKS + _DEBUG_LOGGING_WIRES:
            assert hook not in text, f"生产模块 {source.name} 接上了 {hook}"


async def test_bound_parameter_echo_is_suppressed_only_during_the_credential_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """风险与边界实测：驱动 DEBUG 确实回显绑定参数，凭据写入窗口内全量 records 零命中。

    同一个连接、同一个 root DEBUG 级别下的四段对比，证明边界既有效也不泄漏：
    1. 正控制：不经安全窗口下发与凭据写入同形态的语句（Key 作绑定参数）→
       ``aiosqlite`` DEBUG 记录确实带出明文 Key（风险是真的，不是臆想）。
    2. 边界：同样的 Key 经 SettingRepo.set_provider_api_key 写入 → 任何 logger
       零命中，且存储值确实被写了（不是“没写所以没泄露”）。
    3. 不泄漏：退出写入后驱动 logger 回到原状态，再一次不经压制的写入又能回显，
       证明压制只在窗口内、finally 确实恢复，而不是遗留的永久禁用状态。
    4. 并发：两个凭据写入窗口重叠（单锁串行化语句，不串行化窗口）时仍零命中。
    """
    raw_key = fake_api_key("echo-raw")
    repo_key = fake_api_key("echo-repo")
    after_key = fake_api_key("echo-after")
    concurrent_a = fake_api_key("echo-conc-a")
    concurrent_b = fake_api_key("echo-conc-b")
    caplog.set_level(logging.DEBUG)

    async with open_database(tmp_path / "app.db") as db:
        settings = SettingRepo(db)

        async def raw_write(secret: str) -> None:
            """同形态语句但绕开安全边界：仅用于证明参数回显风险真实存在。"""

            async def op(conn: aiosqlite.Connection) -> None:
                await conn.execute(
                    "INSERT INTO provider_config (provider, api_key, updated_at)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(provider) DO UPDATE SET api_key = excluded.api_key,"
                    " updated_at = excluded.updated_at",
                    (DEFAULT_PROVIDER, secret, "2026-09-09T12:00:00+00:00"),
                )

            await db.under_lock(op)

        # 1) 正控制：不经压制窗口 → 参数回显型 DEBUG 日志带出明文 Key。
        await raw_write(raw_key)
        assert loggers_with_key(caplog, raw_key) == {DEFAULT_ECHOING_LOGGER}
        assert await settings.get_provider_api_key_internal(DEFAULT_PROVIDER) == raw_key

        # 2) 边界：同一连接、同一 DEBUG 级别，经 SettingRepo 写入 → 全量 records 零命中。
        await settings.set_provider_api_key(DEFAULT_PROVIDER, repo_key)
        assert_no_key_in_logs(caplog, repo_key)
        assert (
            await settings.get_provider_api_key_internal(DEFAULT_PROVIDER) == repo_key
        )
        assert_echoing_logger_untouched()

        # 3) 不泄漏：压制已随窗口结束，驱动诊断能力回到原样。
        await raw_write(after_key)
        assert loggers_with_key(caplog, after_key) == {DEFAULT_ECHOING_LOGGER}
        assert (
            await settings.get_provider_api_key_internal(DEFAULT_PROVIDER) == after_key
        )

        # 4) 并发边界：两个写入窗口重叠，先退出的不得替后退出的恢复级别。
        await asyncio.gather(
            settings.set_provider_api_key(DEFAULT_PROVIDER, concurrent_a),
            settings.set_provider_api_key(DEFAULT_PROVIDER, concurrent_b),
        )
        assert_no_key_in_logs(caplog, concurrent_a)
        assert_no_key_in_logs(caplog, concurrent_b)
        assert_echoing_logger_untouched()

    assert_echoing_logger_untouched()


async def test_cancel_during_suppression_fence_waits_for_fence_before_restoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """栅栏等待窗口内被取消：先等栅栏 settle 才恢复 logger，再重抛取消（10.3 P1）。

    压制窗口退出时排的无参数栅栏语句，其队列顺序证明凭据语句的尾部 DEBUG 日志已过；
    栅栏要取唯一锁，等锁+等语句这段时间外层可能被取消。旧实现在取锁等待点直接
    丢掉取消并立刻恢复级别，把凭据尾部日志留在解压制窗口里。

    确定性时序（只用 Event，不用时间 sleep）：
    1. 阻塞 UDF 占住 aiosqlite 工作线程；写入任务在持锁状态下停在 BEGIN 等待点。
    2. 门控任务先排入锁等待队列，再释放工作线程：事务释放锁时按 FIFO 交给门控，
       栅栏只能排在门控之后 → 取消确定落在“栅栏等锁”这个等待窗口里。
    3. 取消被投递一次后（哨兵任务定序）断言：任务未完成、logger 仍处压制态、
       栅栏语句尚未下发——旧实现在此处已恢复级别并结束任务（红）。
    4. 释放门控 → 栅栏真正跑完 → 才恢复原状态并把取消交给调用方。
    """
    key = fake_api_key("fence-cancel")
    retry_key = fake_api_key("fence-cancel-retry")
    caplog.set_level(logging.DEBUG)  # root DEBUG
    driver = logging.getLogger(DEFAULT_ECHOING_LOGGER)
    # 驱动 logger 也显式 DEBUG（本用例设定的“原状”）：一旦提前恢复就会被捕获到。
    monkeypatch.setattr(driver, "level", logging.DEBUG)
    monkeypatch.setattr(driver, "disabled", False)
    original_state = (driver.level, driver.disabled)

    async with open_database(tmp_path / "app.db") as db:
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
        fence_submitted = asyncio.Event()
        gate_queued = asyncio.Event()
        gate_holds = asyncio.Event()
        release_gate = asyncio.Event()
        real_execute = conn.execute

        def noting_execute(sql: str, *args: Any, **kwargs: Any) -> Any:
            # 同步包装（不新增挂起点）：只能看到语句下发时刻，用于确定时序。
            if sql == "BEGIN":
                begin_submitted.set()
            if sql == "SELECT 1":  # 压制窗口退出时的栅栏语句（无绑定参数）
                fence_submitted.set()
            return real_execute(sql, *args, **kwargs)

        conn.execute = noting_execute  # type: ignore[method-assign]

        # 占住工作线程：此后排入队列的语句（含 BEGIN）都只能等线程空闲。
        blocker_task = asyncio.create_task(conn.execute("SELECT blocker()"))
        await asyncio.to_thread(entered_blocker.wait)

        task = asyncio.create_task(
            SettingRepo(db).set_provider_api_key(DEFAULT_PROVIDER, key)
        )
        await begin_submitted.wait()
        # BEGIN 已下发且工作线程被占住：写入任务持锁停在 BEGIN 等待点。
        assert db._lock.locked()

        async def lock_gate() -> None:
            gate_queued.set()
            async with db._lock:
                gate_holds.set()
                await release_gate.wait()

        gate_task = asyncio.create_task(lock_gate())
        # gate_queued 与随后的取锁之间无挂起点：此刻门控必定已在锁等待队列里。
        await gate_queued.wait()

        release_blocker.set()
        # 事务已把锁交给门控：写入任务只能处在退出路径的栅栏等待里（后面只剩栅栏）。
        await gate_holds.wait()

        task.cancel()
        # 哨兵任务在 cancel 之后入队，它跑过即说明取消已至少被投递一次（纯调度定序）。
        cancellation_delivered = asyncio.Event()

        async def observer() -> None:
            cancellation_delivered.set()

        observer_task = asyncio.create_task(observer())
        await cancellation_delivered.wait()
        await observer_task

        # 取消落在栅栏等待窗口：不得提前恢复 logger，也不得提前结束任务。
        assert not task.done(), "取消把栅栏等待一起抽掉了：任务在栅栏 settle 前就结束"
        assert not fence_submitted.is_set(), "栅栏尚未下发，此时只能仍在等锁"
        assert driver.level == logging.WARNING, (
            f"栅栏未 settle 就恢复了 logger：{driver.level}"
        )
        assert driver.disabled is False

        release_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 取消后栅栏仍然跑完，然后才恢复原状；取消没被吞掉也没把栅栏弄丢。
        assert fence_submitted.is_set()
        assert (driver.level, driver.disabled) == original_state
        assert not db._lock.locked()
        # 全量 logging records（root + 驱动 DEBUG）零 Key。
        assert_no_key_in_logs(caplog, key)
        await gate_task
        await blocker_task

        # 取消发生在提交之后：凭据写入不回滚、库仍可用；压制计数也没泄漏给下一个窗口。
        repo = SettingRepo(db)
        assert await repo.get_provider_api_key_internal(DEFAULT_PROVIDER) == key
        await repo.set_provider_api_key(DEFAULT_PROVIDER, retry_key)
        assert_no_key_in_logs(caplog, retry_key)
        assert (driver.level, driver.disabled) == original_state


def test_fake_keys_are_runtime_generated_and_not_real_credentials() -> None:
    """假 Key 由运行时随机生成、辨识度明确，且不取自环境变量里的真实凭据。"""
    generated = {fake_api_key("gen") for _ in range(50)}
    assert len(generated) == 50, "假 Key 必须每次运行现取随机值"
    assert all(
        item.startswith("sk-fitagent-fake-gen-") and len(item) >= 48
        for item in generated
    )
    env_values = {value for value in os.environ.values() if value}
    assert not any(item in env_values for item in generated), "假 Key 不得来自环境变量"
