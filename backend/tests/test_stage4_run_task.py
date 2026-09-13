"""S4-03：执行驱动、取消与 draining（stage4.md S4-03 验收）。

可控阻塞桩（asyncio.Event 握手，无 sleep 轮询、无真实模型请求）覆盖：pending 取消、
running 取消、终态重复取消、cancel/complete 与 cancel/fail 竞态、底层延迟退出时的
draining busy、无观察者（SSE 断开等价）不触发取消、已发生副作用不伪回滚，以及
无后台任务／名额泄漏。

边界：SSE 归 S4-07——本片没有消费者接缝，「SSE 断开不取消」只以「无观察者时执行照常
推进到 completed」留证；摘要与草稿就绪通知归 S4-06/S4-07，本片以「取消后除 user_request
与 cancelled 事件外零写入」留证。
"""

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart

from runtime.error_codes import MODEL_REQUEST_TIMEOUT, RUN_TIMEOUT
from runtime.run_service import RunService
from runtime.run_task import (
    ActiveExecution,
    ExecutionDriver,
    ExecutionFailure,
    FrameworkMessages,
)
from storage.errors import ConversationBusy, RunStateConflict
from storage.run_repo import RunRepo
from tests.support import dump_framework_message, open_database


def _messages(text: str = "完整回答") -> FrameworkMessages:
    return [("assistant", dump_framework_message(ModelResponse([TextPart(text)])))]


async def _pending_run(
    repo: RunRepo,
    run_id: str,
    client_request_id: str,
    *,
    conversation_id: str = "c1",
) -> None:
    if await repo.get_conversation(conversation_id) is None:
        await repo.create_conversation(conversation_id)
    await RunService(repo).submit_request(
        conversation_id=conversation_id,
        run_id=run_id,
        client_request_id=client_request_id,
        text="帮我安排训练",
    )


async def _run(repo: RunRepo, run_id: str) -> dict:
    """按身份读取 Run 并断言存在（避免在断言里对 Optional 下标）。"""
    run = await repo.get_run(run_id)
    assert run is not None, f"Run 不存在: {run_id}"
    return run


async def _kinds(repo: RunRepo, run_id: str) -> list[str]:
    return [str(m["kind"]) for m in await repo.list_run_messages(run_id)]


async def _events(repo: RunRepo, run_id: str) -> list[str]:
    return [str(e["event_type"]) for e in await repo.list_run_events(run_id)]


async def _active_run_ids(db) -> list[str]:
    """库内 ``pending``/``running`` Run：draining 期间应为空，busy 只来自进程内名额。"""

    async def op(conn):
        async with conn.execute(
            "SELECT id FROM runs WHERE status IN ('pending', 'running') ORDER BY id"
        ) as cursor:
            return [str(row["id"]) for row in await cursor.fetchall()]

    return await db.under_lock(op)


def _tracked_tasks() -> set[asyncio.Task]:
    """除当前测试任务外仍未结束的 asyncio 任务（检测后台任务泄漏）。"""
    current = asyncio.current_task()
    return {t for t in asyncio.all_tasks() if t is not current and not t.done()}


def _assert_no_leaks(driver: ExecutionDriver, baseline: set[asyncio.Task]) -> None:
    assert driver.active_run_id is None
    assert _tracked_tasks() == baseline


# ---------- 验收：pending / running 取消 ----------


async def test_cancel_pending_run_persists_and_never_starts_work(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _pending_run(repo, "r1", "cr-1")
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()
        attempts: list[str] = []

        async def work(active: ActiveExecution) -> FrameworkMessages:
            attempts.append(active.run_id)
            return _messages()

        cancelled = await driver.cancel("r1")
        assert cancelled["status"] == "cancelled"
        # 已取消 Run 迟来的启动：条件写冲突，绝不调用底层执行
        await driver.start("r1", work)
        assert attempts == []
        assert (await _run(repo, "r1"))["status"] == "cancelled"
        assert await _kinds(repo, "r1") == ["user_request"]
        assert await _events(repo, "r1") == ["cancelled"]
        _assert_no_leaks(driver, baseline)


async def test_cancel_between_start_and_first_attempt_prevents_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel 与 pending→running 竞争：状态已进入 running 但底层调用尚未启动时取消，
    仍不得启动任何模型／工具尝试。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _pending_run(repo, "r1", "cr-1")
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()
        started_running = asyncio.Event()
        gate = asyncio.Event()
        original_start = repo.start_run
        attempts: list[str] = []

        async def gated_start(run_id):
            run = await original_start(run_id)
            started_running.set()
            await gate.wait()
            return run

        monkeypatch.setattr(repo, "start_run", gated_start)

        async def work(active: ActiveExecution) -> FrameworkMessages:
            attempts.append(active.run_id)
            return _messages()

        task = driver.start("r1", work)
        await started_running.wait()
        assert (await driver.cancel("r1"))["status"] == "cancelled"
        gate.set()
        await task
        assert attempts == []
        assert (await _run(repo, "r1"))["status"] == "cancelled"
        assert await _kinds(repo, "r1") == ["user_request"]
        assert await _events(repo, "r1") == ["cancelled"]
        _assert_no_leaks(driver, baseline)


async def test_cancel_running_run_interrupts_work_and_keeps_side_effects(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _pending_run(repo, "r1", "cr-1")
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()
        started = asyncio.Event()
        interrupted = asyncio.Event()

        async def work(active: ActiveExecution) -> FrameworkMessages:
            # 已发生的业务副作用代理：取消不伪装回滚，也不补写「已回滚」痕迹
            await repo.append_run_events(
                active.run_id, [("draft_ready", {"draft_id": "d1"})]
            )
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            return _messages()

        task = driver.start("r1", work)
        await started.wait()
        assert (await _run(repo, "r1"))["status"] == "running"

        assert (await driver.cancel("r1"))["status"] == "cancelled"
        await task
        assert interrupted.is_set()
        assert await _kinds(repo, "r1") == ["user_request"]  # 无迟到 Assistant 成功消息
        assert await _events(repo, "r1") == ["draft_ready", "cancelled"]
        assert (await _run(repo, "r1"))["status"] == "cancelled"
        _assert_no_leaks(driver, baseline)


async def test_repeat_cancel_of_terminal_runs_is_rejected_without_extra_writes(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()

        async def success(active: ActiveExecution) -> FrameworkMessages:
            return _messages()

        async def failing(active: ActiveExecution) -> FrameworkMessages:
            raise ExecutionFailure(RUN_TIMEOUT)

        await _pending_run(repo, "r1", "cr-1")
        assert (await driver.cancel("r1"))["status"] == "cancelled"

        await _pending_run(repo, "r2", "cr-2")
        await driver.start("r2", success)

        await _pending_run(repo, "r3", "cr-3")
        await driver.start("r3", failing)

        for run_id in ("r1", "r2", "r3"):
            with pytest.raises(RunStateConflict, match="仅 pending/running"):
                await driver.cancel(run_id)

        assert (await _run(repo, "r1"))["status"] == "cancelled"
        assert (await _run(repo, "r2"))["status"] == "completed"
        failed = await _run(repo, "r3")
        assert failed["status"] == "failed" and failed["error_code"] == RUN_TIMEOUT
        assert await _events(repo, "r1") == ["cancelled"]  # 重复取消不追加事件
        assert await _events(repo, "r2") == []
        assert await _events(repo, "r3") == ["failed"]
        assert await _kinds(repo, "r2") == ["user_request", "framework"]
        _assert_no_leaks(driver, baseline)


# ---------- 验收：底层延迟退出（draining）仍占名额 ----------


async def test_low_level_exit_delays_slot_release_and_keeps_requests_busy(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _pending_run(repo, "r1", "cr-1")
        driver = ExecutionDriver(repo)
        service = RunService(repo, active_execution=lambda: driver.active_run_id)
        baseline = _tracked_tasks()
        low_level_started = asyncio.Event()
        low_level_exit = asyncio.Event()
        attempts: list[str] = []

        async def work(active: ActiveExecution) -> FrameworkMessages:
            attempts.append("attempt-1")
            low_level_started.set()
            # 本次调用不可安全中断：吞掉取消，等它实际退出（08 8.3）
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.Event().wait()  # 当前模型／工具调用
            await low_level_exit.wait()
            if active.cancel_requested:
                return _messages("迟到的成功")  # 迟到结果不得落库
            attempts.append("attempt-2")  # 取消后仍启动新尝试 = 反例
            return _messages()

        task = driver.start("r1", work)
        await low_level_started.wait()
        assert (await driver.cancel("r1"))["status"] == "cancelled"

        # draining：库内已无活跃 Run，但名额未释放，新请求仍 busy
        assert driver.active_run_id == "r1"
        assert await _active_run_ids(db) == []
        with pytest.raises(ConversationBusy, match="r1"):
            await service.submit_request(
                conversation_id="c1",
                run_id="r2",
                client_request_id="cr-2",
                text="第二个请求",
            )
        # 幂等查重仍先于 busy：同一 client_request_id 返回已有（已取消）Run
        again = await service.submit_request(
            conversation_id="c1",
            run_id="r3",
            client_request_id="cr-1",
            text="帮我安排训练",
        )
        assert again["created"] is False and again["run"]["id"] == "r1"
        with pytest.raises(RuntimeError, match="已被占用"):
            driver.start("r2", work)

        low_level_exit.set()
        await task
        assert attempts == ["attempt-1"]
        assert driver.active_run_id is None
        assert (await _run(repo, "r1"))["status"] == "cancelled"
        assert await _kinds(repo, "r1") == ["user_request"]
        assert await _events(repo, "r1") == ["cancelled"]

        # 底层调用实际退出后才释放名额：新请求可再次创建
        created = await service.submit_request(
            conversation_id="c1",
            run_id="r4",
            client_request_id="cr-4",
            text="第三个请求",
        )
        assert created["created"] is True and created["run"]["status"] == "pending"
        _assert_no_leaks(driver, baseline)


# ---------- 验收：cancel/complete、cancel/fail 竞态 ----------


async def test_cancel_wins_completion_race_and_drops_late_assistant_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _pending_run(repo, "r1", "cr-1")
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()
        about_to_commit = asyncio.Event()
        gate = asyncio.Event()
        original_complete = repo.complete_run

        async def gated_complete(run_id, messages):
            about_to_commit.set()
            await gate.wait()
            return await original_complete(run_id, messages)

        monkeypatch.setattr(repo, "complete_run", gated_complete)

        async def work(active: ActiveExecution) -> FrameworkMessages:
            return _messages()

        task = driver.start("r1", work)
        await about_to_commit.wait()
        # 完成结果已产出、尚未提交时用户取消：只有取消能提交
        assert (await driver.cancel("r1"))["status"] == "cancelled"
        gate.set()
        await task

        assert (await _run(repo, "r1"))["status"] == "cancelled"
        assert await _kinds(repo, "r1") == ["user_request"]
        assert await _events(repo, "r1") == ["cancelled"]
        _assert_no_leaks(driver, baseline)


async def test_cancel_wins_failure_race_then_reverse_keeps_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()
        about_to_fail = asyncio.Event()
        gate = asyncio.Event()
        original_fail = repo.fail_run

        async def gated_fail(run_id, error_code):
            about_to_fail.set()
            await gate.wait()
            return await original_fail(run_id, error_code)

        monkeypatch.setattr(repo, "fail_run", gated_fail)

        async def failing(active: ActiveExecution) -> FrameworkMessages:
            raise ExecutionFailure(MODEL_REQUEST_TIMEOUT)

        await _pending_run(repo, "r1", "cr-1")
        task = driver.start("r1", failing)
        await about_to_fail.wait()
        # 失败结果已产出、尚未提交时用户取消：只有取消能提交
        assert (await driver.cancel("r1"))["status"] == "cancelled"
        gate.set()
        await task
        run = await _run(repo, "r1")
        assert run["status"] == "cancelled" and run["error_code"] is None
        assert await _events(repo, "r1") == ["cancelled"]  # 不追加失败事件

        # 反向：失败先提交，随后取消被条件写拒绝，终态仍只有一个
        await _pending_run(repo, "r2", "cr-2")
        await driver.start("r2", failing)
        with pytest.raises(RunStateConflict, match="仅 pending/running"):
            await driver.cancel("r2")
        second = await _run(repo, "r2")
        assert second["status"] == "failed"
        assert second["error_code"] == MODEL_REQUEST_TIMEOUT
        assert await _events(repo, "r2") == ["failed"]
        _assert_no_leaks(driver, baseline)


async def test_unclassified_work_exception_releases_slot_without_inventing_code(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _pending_run(repo, "r1", "cr-1")
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()

        async def broken(active: ActiveExecution) -> FrameworkMessages:
            raise RuntimeError("boom: 未分类缺陷")

        task = driver.start("r1", broken)
        with pytest.raises(RuntimeError, match="boom"):
            await task
        assert driver.active_run_id is None
        # 未分类异常不映射成任何错误码：Run 停在 running，由启动恢复收口
        run = await _run(repo, "r1")
        assert run["status"] == "running" and run["error_code"] is None
        assert await _events(repo, "r1") == []
        _assert_no_leaks(driver, baseline)


# ---------- 验收：无观察者（SSE 断开等价）不触发取消 ----------


async def test_execution_completes_without_any_observer(tmp_path: Path) -> None:
    """本片没有 SSE/消费者接缝（S4-07 才有），断开等价物没有可触发的取消路径；
    没有观察者读取事件与消息时，执行照常推进到 completed。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _pending_run(repo, "r1", "cr-1")
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()

        async def work(active: ActiveExecution) -> FrameworkMessages:
            assert await repo.list_run_events(active.run_id) == []  # 无人在场
            return _messages()

        await driver.start("r1", work)
        run = await _run(repo, "r1")
        assert run["status"] == "completed" and run["error_code"] is None
        assert await _kinds(repo, "r1") == ["user_request", "framework"]
        assert json.loads((await repo.list_framework_messages("r1"))[0]["payload_json"])
        _assert_no_leaks(driver, baseline)


async def test_driver_holds_the_execution_task_until_slot_release(
    tmp_path: Path,
) -> None:
    """S4-07：生产入口丢弃 ``start`` 的返回值，驱动必须自己持有执行任务强引用。

    ``asyncio`` 只对任务保持弱引用（无强引用时可能在执行中被 GC）：本用例白盒断言引用在
    名额占用期间存在、在名额释放时一并解除。
    """
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _pending_run(repo, "r1", "cr-1")
        driver = ExecutionDriver(repo)
        baseline = _tracked_tasks()
        running = asyncio.Event()
        gate = asyncio.Event()

        async def work(active: ActiveExecution) -> FrameworkMessages:
            running.set()
            await gate.wait()
            return _messages()

        task = driver.start("r1", work)
        await running.wait()
        active = driver._active
        assert active is not None
        assert active.execution_task is task and not task.done()
        gate.set()
        await task
        assert active.execution_task is None
        assert driver.active_run_id is None
        _assert_no_leaks(driver, baseline)
