"""S4-06a：摘要持久化 schema 与仓库契约（stage4.md S4-06；07 7.4「Stage 4 已拍：摘要持久化」）。

覆盖：提交成功才启用、覆盖范围精确、来源关联可追溯、原消息一条不删、最新有效摘要确定、
提交失败整体回滚并上抛、取消与提交两种次序（取消先发生不启用、不补写诊断快照）。
迁移 014 的前向与失败回滚在 ``tests/test_migrations.py``。

边界：本片不生成模型摘要、不做上下文投影、不发真实模型请求；临时库 + 离线确定性用例。
"""

import sqlite3
from pathlib import Path

import pytest

from storage.errors import InvalidInput, NotFound, RunStateConflict
from storage.run_repo import RunRepo
from storage.summary_repo import SummaryRepo
from tests.support import dump_framework_message, open_database, text_response


async def _count_summaries(db) -> int:
    """摘要行数（固定语句：测试不拼接 SQL）。"""

    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM summaries") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _count_summary_sources(db) -> int:
    """来源关联行数（固定语句）。"""

    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM summary_sources") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _count_run_events(db) -> int:
    """轨迹事件总数（固定语句）：取消先提交后不得有任何迟到诊断快照。"""

    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM run_events") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _finish_run(repo: RunRepo, conversation_id: str, index: int) -> str:
    """创建并完成一个 Run：给会话追加 user_request + framework 两条消息（seq = 2i-1, 2i）。"""
    run_id = f"r{index}"
    await repo.create_run_with_user_message(
        conversation_id, run_id, f"cr-{index}", f"请求-{index}"
    )
    await repo.start_run(run_id)
    await repo.complete_run(
        run_id, [("assistant", dump_framework_message(text_response(f"回答-{index}")))]
    )
    return run_id


async def _start_run(repo: RunRepo, conversation_id: str, index: int) -> str:
    """创建并启动一个 Run（停在 running）；给会话追加 user_request 一条消息。"""
    run_id = f"r{index}"
    await repo.create_run_with_user_message(
        conversation_id, run_id, f"cr-{index}", f"请求-{index}"
    )
    await repo.start_run(run_id)
    return run_id


async def test_commit_persists_coverage_sources_and_keeps_originals(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        summaries = SummaryRepo(db)
        await runs.create_conversation("c1")
        await _finish_run(runs, "c1", 1)
        await _finish_run(runs, "c1", 2)
        running = await _start_run(runs, "c1", 3)
        before = await runs.list_messages("c1")
        assert [row["seq"] for row in before] == [1, 2, 3, 4, 5]
        source_ids = [row["id"] for row in before if row["seq"] <= 4]

        committed = await summaries.commit_summary(
            run_id=running,
            content="旧历史摘要",
            covered_from_seq=1,
            covered_to_seq=4,
            source_message_ids=source_ids,
        )
        assert committed["conversation_id"] == "c1"
        assert committed["run_id"] == running
        assert committed["content"] == "旧历史摘要"
        assert (committed["covered_from_seq"], committed["covered_to_seq"]) == (1, 4)
        assert isinstance(committed["created_at"], str) and committed["created_at"]

        active = await summaries.get_active_summary("c1")
        assert active is not None
        assert active["id"] == committed["id"]
        assert active["content"] == "旧历史摘要"
        assert await summaries.list_source_message_ids(committed["id"]) == sorted(
            source_ids
        )

        # 原消息一条不删、内容不变：摘要是派生的追加行，不替代消息存储
        assert await runs.list_messages("c1") == before
        # 结构层保证来源不悬空：被 summary_sources 引用的原消息删不掉（外键拒绝）
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "DELETE FROM messages WHERE id = ?", (source_ids[0],)
                )


async def test_commit_requires_running_owning_run(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        summaries = SummaryRepo(db)
        await runs.create_conversation("c1")
        await runs.create_run_with_user_message("c1", "r1", "cr-1", "请求")
        source_ids = [row["id"] for row in await runs.list_messages("c1")]
        args = {
            "content": "摘要",
            "covered_from_seq": 1,
            "covered_to_seq": 1,
            "source_message_ids": source_ids,
        }
        # pending：尚未开始执行的 Run 不能启用摘要
        with pytest.raises(RunStateConflict, match="pending"):
            await summaries.commit_summary(run_id="r1", **args)
        await runs.start_run("r1")
        await runs.complete_run(
            "r1", [("assistant", dump_framework_message(text_response("回答")))]
        )
        # completed：终态 Run 不能启用摘要（迟到摘要不落库）
        with pytest.raises(RunStateConflict, match="completed"):
            await summaries.commit_summary(run_id="r1", **args)
        assert await _count_summaries(db) == 0
        assert await _count_summary_sources(db) == 0


async def test_cancel_first_commit_writes_nothing(tmp_path: Path) -> None:
    """取消先提交：提交被拒、无有效摘要、不补写迟到诊断快照（07 7.4）。"""
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        summaries = SummaryRepo(db)
        await runs.create_conversation("c1")
        await _finish_run(runs, "c1", 1)
        running = await _start_run(runs, "c1", 2)
        message_ids = [row["id"] for row in await runs.list_messages("c1")]
        await runs.cancel_run(running)
        events_before = await _count_run_events(db)

        with pytest.raises(RunStateConflict, match="cancelled"):
            await summaries.commit_summary(
                run_id=running,
                content="迟到摘要",
                covered_from_seq=1,
                covered_to_seq=2,
                source_message_ids=message_ids,
            )
        assert await summaries.get_active_summary("c1") is None
        assert await _count_summaries(db) == 0
        assert await _count_summary_sources(db) == 0
        # 只保留取消事件本身，不追加任何诊断快照
        assert await _count_run_events(db) == events_before


async def test_commit_first_then_cancel_keeps_committed_summary(tmp_path: Path) -> None:
    """提交先发生：摘要已是既成事实，随后的取消不改写、不删除它（取消不追认撤销已提交写入）。"""
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        summaries = SummaryRepo(db)
        await runs.create_conversation("c1")
        await _finish_run(runs, "c1", 1)
        running = await _start_run(runs, "c1", 2)
        message_ids = [
            row["id"] for row in await runs.list_messages("c1") if row["seq"] <= 2
        ]

        committed = await summaries.commit_summary(
            run_id=running,
            content="已提交摘要",
            covered_from_seq=1,
            covered_to_seq=2,
            source_message_ids=message_ids,
        )
        cancelled = await runs.cancel_run(running)
        assert cancelled["status"] == "cancelled"

        active = await summaries.get_active_summary("c1")
        assert active is not None and active["id"] == committed["id"]
        assert await summaries.list_source_message_ids(committed["id"]) == sorted(
            message_ids
        )
        assert await _count_summaries(db) == 1


async def test_latest_active_is_deterministic_and_extends_coverage(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        summaries = SummaryRepo(db)
        await runs.create_conversation("c1")
        await _finish_run(runs, "c1", 1)
        await _finish_run(runs, "c1", 2)
        first_run = await _start_run(runs, "c1", 3)
        rows = await runs.list_messages("c1")
        first_sources = [row["id"] for row in rows if row["seq"] <= 4]
        first = await summaries.commit_summary(
            run_id=first_run,
            content="第一版摘要",
            covered_from_seq=1,
            covered_to_seq=4,
            source_message_ids=first_sources,
        )
        await runs.complete_run(
            first_run, [("assistant", dump_framework_message(text_response("回答-3")))]
        )
        second_run = await _start_run(runs, "c1", 4)
        rows = await runs.list_messages("c1")
        second_sources = [row["id"] for row in rows if row["seq"] in (5, 6)]
        second = await summaries.commit_summary(
            run_id=second_run,
            content="第二版摘要（含第一版覆盖与更近的历史）",
            covered_from_seq=1,
            covered_to_seq=6,
            source_message_ids=second_sources,
        )

        active = await summaries.get_active_summary("c1")
        assert active is not None and active["id"] == second["id"]
        # 旧摘要不删除、来源关联不变：追加不覆盖
        listed = await summaries.list_summaries("c1")
        assert [row["id"] for row in listed] == [first["id"], second["id"]]
        assert await summaries.list_source_message_ids(first["id"]) == sorted(
            first_sources
        )
        assert await summaries.list_source_message_ids(second["id"]) == sorted(
            second_sources
        )
        # 原消息全部保留（含已被两版摘要覆盖的部分）：r1–r2 各 2 条、r3 用户+框架 2 条、r4 当前用户 1 条
        assert len(await runs.list_messages("c1")) == 7


async def test_commit_rejects_non_extending_or_shifted_coverage(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        summaries = SummaryRepo(db)
        await runs.create_conversation("c1")
        await _finish_run(runs, "c1", 1)
        first_run = await _start_run(runs, "c1", 2)
        rows = await runs.list_messages("c1")
        await summaries.commit_summary(
            run_id=first_run,
            content="第一版摘要",
            covered_from_seq=1,
            covered_to_seq=2,
            source_message_ids=[row["id"] for row in rows if row["seq"] <= 2],
        )
        await runs.complete_run(
            first_run, [("assistant", dump_framework_message(text_response("回答-2")))]
        )
        second_run = await _start_run(runs, "c1", 3)
        rows = await runs.list_messages("c1")
        message_ids = [row["id"] for row in rows]
        # 覆盖必须向前扩展：终点不前进即拒绝
        with pytest.raises(InvalidInput, match="扩展"):
            await summaries.commit_summary(
                run_id=second_run,
                content="重复覆盖",
                covered_from_seq=1,
                covered_to_seq=2,
                source_message_ids=message_ids[:2],
            )
        # 覆盖必须是会话前缀：从中间起会静默丢掉更早消息，拒绝
        with pytest.raises(InvalidInput, match="最早未摘要"):
            await summaries.commit_summary(
                run_id=second_run,
                content="丢历史",
                covered_from_seq=4,
                covered_to_seq=5,
                source_message_ids=message_ids,
            )
        # 合法扩展仍可用
        committed = await summaries.commit_summary(
            run_id=second_run,
            content="第二版摘要",
            covered_from_seq=1,
            covered_to_seq=3,
            source_message_ids=message_ids[:3],
        )
        active = await summaries.get_active_summary("c1")
        assert active is not None and active["id"] == committed["id"]
        assert await _count_summaries(db) == 2


async def test_commit_rejects_invalid_input(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        summaries = SummaryRepo(db)
        await runs.create_conversation("c1")
        running = await _start_run(runs, "c1", 1)
        await runs.save_partial_answer(running, "部分回答")
        message_ids = [row["id"] for row in await runs.list_messages("c1")]
        assert len(message_ids) == 2

        with pytest.raises(InvalidInput, match="内容"):
            await summaries.commit_summary(
                run_id=running,
                content="",
                covered_from_seq=1,
                covered_to_seq=1,
                source_message_ids=message_ids[:1],
            )
        with pytest.raises(InvalidInput, match="起点"):
            await summaries.commit_summary(
                run_id=running,
                content="摘要",
                covered_from_seq=0,
                covered_to_seq=1,
                source_message_ids=message_ids[:1],
            )
        with pytest.raises(InvalidInput, match="不合法"):
            await summaries.commit_summary(
                run_id=running,
                content="摘要",
                covered_from_seq=2,
                covered_to_seq=1,
                source_message_ids=message_ids[:1],
            )
        # 区间必须完整落在已保存消息上（不能指向不存在的 seq）
        with pytest.raises(InvalidInput, match="完整落在"):
            await summaries.commit_summary(
                run_id=running,
                content="摘要",
                covered_from_seq=1,
                covered_to_seq=3,
                source_message_ids=message_ids,
            )
        # 来源必须落在覆盖区间内、不得重复、不得为空
        with pytest.raises(InvalidInput, match="不在覆盖区间"):
            await summaries.commit_summary(
                run_id=running,
                content="摘要",
                covered_from_seq=1,
                covered_to_seq=1,
                source_message_ids=message_ids,
            )
        with pytest.raises(InvalidInput, match="重复"):
            await summaries.commit_summary(
                run_id=running,
                content="摘要",
                covered_from_seq=1,
                covered_to_seq=1,
                source_message_ids=[message_ids[0], message_ids[0]],
            )
        with pytest.raises(InvalidInput, match="至少关联一条"):
            await summaries.commit_summary(
                run_id=running,
                content="摘要",
                covered_from_seq=1,
                covered_to_seq=1,
                source_message_ids=[],
            )
        # 不存在的 Run：显式 NotFound，不落任何行
        with pytest.raises(NotFound, match="r-missing"):
            await summaries.commit_summary(
                run_id="r-missing",
                content="摘要",
                covered_from_seq=1,
                covered_to_seq=1,
                source_message_ids=[message_ids[0]],
            )
        assert await _count_summaries(db) == 0
        assert await _count_summary_sources(db) == 0


async def test_commit_failure_rolls_back_atomically_and_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """来源关联步骤注入失败：摘要行与来源一起回滚，异常上抛（存储失败不得降级继续）。"""
    async with open_database(tmp_path / "app.db") as db:
        runs = RunRepo(db)
        summaries = SummaryRepo(db)
        await runs.create_conversation("c1")
        running = await _start_run(runs, "c1", 1)
        message_ids = [row["id"] for row in await runs.list_messages("c1")]

        async def failing_sources(conn, *, summary_id, message_ids):
            raise RuntimeError("boom: 来源关联步骤失败")

        monkeypatch.setattr(
            summaries, "_insert_summary_sources_locked", failing_sources
        )
        with pytest.raises(RuntimeError, match="boom"):
            await summaries.commit_summary(
                run_id=running,
                content="摘要",
                covered_from_seq=1,
                covered_to_seq=1,
                source_message_ids=message_ids,
            )
        monkeypatch.undo()
        assert await _count_summaries(db) == 0
        assert await _count_summary_sources(db) == 0
        # 事务可继续使用：重试提交成功，Run 未受影响
        committed = await summaries.commit_summary(
            run_id=running,
            content="重试摘要",
            covered_from_seq=1,
            covered_to_seq=1,
            source_message_ids=message_ids,
        )
        active = await summaries.get_active_summary("c1")
        assert active is not None and active["id"] == committed["id"]
        run = await runs.get_run(running)
        assert run is not None and run["status"] == "running"
