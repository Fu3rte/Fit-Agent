"""S0-06：运行时存储操作与原子性（stage0.md 第 5 节 S0-06 八条验收）。

覆盖（编号对应 stage0.md S0-06 验收标准 1–8）：
1. 用户消息+pending Run 同事务原子创建；client_request_id 重复/并发全局幂等。
2. 仅 running Run 可同事务写完整框架消息并改 completed；中途失败整体回滚。
3. 取消条件更新（pending/running→cancelled）+ 同事务取消事件；取消后迟到完成拒写。
4. 完成/取消可控并发竞争不能同时成功；取消失败不残留"取消成功"事件。
5. 重开数据库读取会话/消息/Run/事件/部分回答；partial 与 framework 可区分。
6. retry_of_run_id 指针可保存（不实现重试调度）。
7. PydanticAI 原生 JSON 往返、多请求/响应与工具调用/结果顺序、本 Run 增量与用户请求关联。
8. 取消先提交后，晚到框架快照/迟到成功结果/部分回答拒写，此前记录仍可读。

存储层离线测试：假消息 + 临时文件库；不声称已验证实际流式断线、模型取消或
服务重启恢复（stage0.md 第 6 节覆盖边界）。终态后不补写轨迹、不补存晚到
框架快照的边界由 append_run_events / save_partial_answer / complete_run 的
条件检查保证（07 7.4 消息契约）。
"""

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import ToolCallPart, ToolReturnPart

from storage.errors import InvalidInput, NotFound, RunStateConflict
from storage.run_repo import RunRepo
from tests.support import (
    dump_framework_message,
    load_framework_message,
    open_database,
    text_response,
    tool_call_response,
    tool_return_request,
    user_request,
)


async def _count(db, sql: str, params: tuple = ()) -> int:
    """测试专用计数读取：仍经唯一锁访问，不在锁外直接操作连接。"""

    async def op(conn):
        async with conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _make_running_run(repo: RunRepo, conversation_id: str = "c1", run_id: str = "r1") -> str:
    await repo.create_conversation(conversation_id)
    await repo.create_run_with_user_message(
        conversation_id, run_id, f"cr-{run_id}", f"请求-{run_id}"
    )
    await repo.start_run(run_id)
    return run_id


# ---------- 验收 1：用户消息+pending 同事务原子创建，全局幂等 ----------


async def test_user_message_and_pending_run_created_together(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        result = await repo.create_run_with_user_message("c1", "r1", "cr-1", "帮我安排训练")
        assert result["created"] is True
        assert result["run"]["status"] == "pending"

        messages = await repo.list_messages("c1")
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["kind"] == "user_request"
        assert messages[0]["run_id"] == "r1"


async def test_duplicate_client_request_id_returns_existing_run(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        first = await repo.create_run_with_user_message("c1", "r1", "cr-1", "第一条")
        again = await repo.create_run_with_user_message("c1", "r2", "cr-1", "第一条")

        assert again["created"] is False
        assert again["run"]["id"] == first["run"]["id"]
        # 只产生一组记录：1 个 Run、1 条用户消息（第二个 run_id 未落库）
        assert await _count(db, "SELECT COUNT(*) FROM runs") == 1
        assert await _count(db, "SELECT COUNT(*) FROM messages") == 1


async def test_concurrent_same_client_request_id_creates_one_set(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        results = await asyncio.gather(
            repo.create_run_with_user_message("c1", "rA", "cr-same", "并发请求"),
            repo.create_run_with_user_message("c1", "rB", "cr-same", "并发请求"),
        )
        created = [r for r in results if r["created"]]
        assert len(created) == 1
        winner_id = created[0]["run"]["id"]
        assert all(r["run"]["id"] == winner_id for r in results)
        assert await _count(db, "SELECT COUNT(*) FROM runs") == 1
        assert await _count(db, "SELECT COUNT(*) FROM messages") == 1


async def test_mid_transaction_failure_leaves_neither_run_nor_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")

        async def failing_insert(conn, *, conversation_id, run_id, user_text):
            raise RuntimeError("boom: 用户消息步骤失败")

        monkeypatch.setattr(repo, "_insert_user_message_locked", failing_insert)
        with pytest.raises(RuntimeError, match="boom"):
            await repo.create_run_with_user_message("c1", "r1", "cr-1", "请求")
        monkeypatch.undo()  # 恢复真实插入，验证事务可继续使用
        # 中途失败两者均不新增，事务可继续使用
        assert await _count(db, "SELECT COUNT(*) FROM runs") == 0
        assert await _count(db, "SELECT COUNT(*) FROM messages") == 0
        result = await repo.create_run_with_user_message("c1", "r2", "cr-2", "重试请求")
        assert result["created"] is True


# ---------- 验收 2：仅 running 可同事务完整提交 completed ----------


async def test_complete_requires_running_status(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message("c1", "r1", "cr-1", "请求")
        payload = dump_framework_message(text_response("回答"))
        with pytest.raises(RunStateConflict, match="仅 running"):
            await repo.complete_run("r1", [("assistant", payload)])
        run = await repo.get_run("r1")
        assert run is not None and run["status"] == "pending"


async def test_complete_run_commits_messages_and_completed_together(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _make_running_run(repo)
        out = await repo.complete_run(
            "r1", [("assistant", dump_framework_message(text_response("回答")))]
        )
        assert out["status"] == "completed"
        assert len(await repo.list_framework_messages("r1")) == 1


async def test_complete_failure_rolls_back_status_and_partial_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _make_running_run(repo)
        original = repo._next_seq_locked
        calls = {"n": 0}

        async def failing_seq(conn, conversation_id):
            calls["n"] += 1
            if calls["n"] >= 2:  # 第二条消息步骤失败：状态已更新+首条已插入后回滚
                raise RuntimeError("boom: 消息步骤失败")
            return await original(conn, conversation_id)

        monkeypatch.setattr(repo, "_next_seq_locked", failing_seq)
        payloads = [
            ("assistant", dump_framework_message(text_response("一"))),
            ("assistant", dump_framework_message(text_response("二"))),
        ]
        with pytest.raises(RuntimeError, match="boom"):
            await repo.complete_run("r1", payloads)
        # 任一步骤失败不得只完成一半：状态回滚、无半套消息
        run = await repo.get_run("r1")
        assert run is not None and run["status"] == "running"
        assert await _count(db, "SELECT COUNT(*) FROM messages") == 1  # 仅用户请求


async def test_complete_run_rejects_empty_or_bad_input(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _make_running_run(repo)
        with pytest.raises(InvalidInput):
            await repo.complete_run("r1", [])
        with pytest.raises(InvalidInput, match="合法 JSON"):
            await repo.complete_run("r1", [("assistant", "{not-json")])


# ---------- 验收 3：取消条件更新+同事务取消事件；取消后迟到完成拒写 ----------


async def test_cancel_from_pending_and_running_writes_event(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message("c1", "r1", "cr-1", "请求一")
        out = await repo.cancel_run("r1", {"reason": "user"})
        assert out["status"] == "cancelled"
        events = await repo.list_run_events("r1")
        assert [e["event_type"] for e in events] == ["cancelled"]
        assert events[0]["payload_json"] == '{"reason": "user"}'

        await repo.create_run_with_user_message("c1", "r2", "cr-2", "请求二")
        await repo.start_run("r2")
        out = await repo.cancel_run("r2")
        assert out["status"] == "cancelled"


async def test_after_cancel_late_complete_rejected(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _make_running_run(repo)
        await repo.cancel_run("r1")

        payload = dump_framework_message(text_response("迟到回答"))
        with pytest.raises(RunStateConflict, match="仅 running"):
            await repo.complete_run("r1", [("assistant", payload)])

        # 终态未被覆盖，无框架消息写入
        run = await repo.get_run("r1")
        assert run is not None and run["status"] == "cancelled"
        assert await repo.list_framework_messages("r1") == []


# ---------- 验收 4：完成/取消确定性竞争 ----------


async def test_complete_and_cancel_cannot_both_succeed(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _make_running_run(repo)
        payload = dump_framework_message(text_response("竞争回答"))

        results = await asyncio.gather(
            repo.complete_run("r1", [("assistant", payload)]),
            repo.cancel_run("r1", {"reason": "race"}),
            return_exceptions=True,
        )
        succeeded = [r for r in results if not isinstance(r, BaseException)]
        conflicts = [r for r in results if isinstance(r, RunStateConflict)]
        assert len(succeeded) == 1 and len(conflicts) == 1

        run = await repo.get_run("r1")
        assert run is not None
        if run["status"] == "completed":
            # 取消失败：不得残留"取消成功"事件，也不得覆盖终态
            assert [e["event_type"] for e in await repo.list_run_events("r1")] == []
            assert len(await repo.list_framework_messages("r1")) == 1
        else:
            assert run["status"] == "cancelled"
            assert [e["event_type"] for e in await repo.list_run_events("r1")] == ["cancelled"]
            assert await repo.list_framework_messages("r1") == []


async def test_failed_cancel_leaves_no_cancelled_event(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _make_running_run(repo)
        await repo.complete_run(
            "r1", [("assistant", dump_framework_message(text_response("回答")))]
        )
        with pytest.raises(RunStateConflict, match="仅 pending/running"):
            await repo.cancel_run("r1")
        assert [e["event_type"] for e in await repo.list_run_events("r1")] == []
        run = await repo.get_run("r1")
        assert run is not None and run["status"] == "completed"


# ---------- 验收 5：重开数据库读取全部记录，partial 与 framework 可区分 ----------


async def test_reopen_reads_conversation_messages_run_events_partials(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message("c1", "r1", "cr-1", "请求")
        await repo.start_run("r1")
        await repo.save_partial_answer("r1", "部分一")
        await repo.save_partial_answer("r1", "部分二")
        await repo.append_run_events("r1", [("progress", {"step": 1})])
        await repo.cancel_run("r1")

    async with open_database(path) as db:
        repo = RunRepo(db)
        assert (await repo.get_conversation("c1")) is not None
        run = await repo.get_run("r1")
        assert run is not None and run["status"] == "cancelled"
        messages = await repo.list_messages("c1")
        kinds = [m["kind"] for m in messages]
        assert kinds == ["user_request", "partial", "partial"]
        # 部分回答可区分于完整成功 Assistant 消息：kind='partial' 独立读取，
        # 本 Run 无 framework 行，完整成功回答只来自 completed Run 的 framework 行
        assert [m["role"] for m in messages if m["kind"] == "partial"] == ["assistant", "assistant"]
        partials = await repo.list_partial_answers("r1")
        assert [p["text"] for p in partials] == ["部分一", "部分二"]
        assert await repo.list_framework_messages("r1") == []
        events = await repo.list_run_events("r1")
        # 此前写入的 progress 事件与同事务取消事件在重开后仍可读
        assert [e["event_type"] for e in events] == ["progress", "cancelled"]


# ---------- 验收 6：retry 指针可保存，不实现重试调度 ----------


async def test_retry_pointer_persisted_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message("c1", "r-old", "cr-old", "旧请求")
        await repo.cancel_run("r-old")
        await repo.create_run_with_user_message(
            "c1", "r-new", "cr-new", "旧请求", retry_of_run_id="r-old"
        )
    async with open_database(path) as db:
        repo = RunRepo(db)
        new_run = await repo.get_run("r-new")
        assert new_run is not None and new_run["retry_of_run_id"] == "r-old"
        # 旧记录不复活
        old_run = await repo.get_run("r-old")
        assert old_run is not None and old_run["status"] == "cancelled"


# ---------- 验收 7：原生 JSON 往返、多请求/响应与工具顺序、本 Run 增量 ----------


async def test_framework_messages_roundtrip_order_and_run_increment(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message("c1", "r1", "cr-1", "查一下训练数据")
        await repo.start_run("r1")
        originals = [
            user_request("查一下训练数据"),
            text_response("先记录调用"),
            tool_call_response("get_plan", {"day": "周一"}, "call-1"),
            tool_return_request("get_plan", "计划内容", "call-1"),
            text_response("最终回答"),
        ]
        roles = ["user", "assistant", "assistant", "user", "assistant"]
        await repo.complete_run(
            "r1",
            [
                (role, dump_framework_message(m))
                for role, m in zip(roles, originals, strict=True)
            ],
        )

    async with open_database(path) as db:
        repo = RunRepo(db)
        rows = await repo.list_framework_messages("r1")
        assert len(rows) == 5
        restored = [load_framework_message(row["payload_json"]) for row in rows]
        # 原生 JSON 往返等价 + 多请求/响应与工具调用/结果顺序保持
        assert restored == originals
        call_part, return_part = restored[2].parts[0], restored[3].parts[0]
        assert isinstance(call_part, ToolCallPart) and isinstance(return_part, ToolReturnPart)
        assert call_part.tool_call_id == return_part.tool_call_id == "call-1"
        assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
        # 用户请求关联：框架行与本 Run 关联，应用事实 user_request 行同属本 Run
        assert {row["run_id"] for row in rows} == {"r1"}
        all_rows = await repo.list_messages("c1")
        assert all_rows[0]["kind"] == "user_request" and all_rows[0]["run_id"] == "r1"
        assert len(all_rows) == 6  # 1 用户请求 + 5 框架消息，无历史重复注入

        # 本 Run 增量：同一会话第二个 Run 只追加自己的新消息
        await repo.create_run_with_user_message("c1", "r2", "cr-2", "第二个请求")
        await repo.start_run("r2")
        await repo.complete_run("r2", [("assistant", dump_framework_message(text_response("答二")))])
        assert len(await repo.list_framework_messages("r1")) == 5
        assert len(await repo.list_framework_messages("r2")) == 1
        assert await _count(db, "SELECT COUNT(*) FROM messages WHERE conversation_id='c1'") == 8


# ---------- 验收 8：取消先提交，晚到快照/成功结果拒写且此前记录可读 ----------


async def test_late_frames_after_committed_cancel_rejected_prior_records_intact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        await repo.create_run_with_user_message("c1", "r1", "cr-1", "请求")
        await repo.start_run("r1")
        await repo.save_partial_answer("r1", "已落盘部分")
        await repo.append_run_events("r1", [("progress", {"step": 1})])
        # 可控时序：取消事务先完整提交
        await repo.cancel_run("r1", {"reason": "user"})

        # 晚到框架运行快照
        with pytest.raises(RunStateConflict):
            await repo.append_run_events("r1", [("framework_snapshot", {"messages": ["late"]})])
        # 迟到成功结果
        late_payload = dump_framework_message(text_response("迟到成功回答"))
        with pytest.raises(RunStateConflict):
            await repo.complete_run("r1", [("assistant", late_payload)])
        # 晚到部分回答批次
        with pytest.raises(RunStateConflict):
            await repo.save_partial_answer("r1", "迟到片段")
        # 迟到取消（终态不可覆盖）
        with pytest.raises(RunStateConflict):
            await repo.cancel_run("r1")

    async with open_database(path) as db:
        repo = RunRepo(db)
        run = await repo.get_run("r1")
        assert run is not None and run["status"] == "cancelled"
        # 无新增框架快照、成功回答或终态覆盖
        assert await repo.list_framework_messages("r1") == []
        events = await repo.list_run_events("r1")
        assert [e["event_type"] for e in events] == ["progress", "cancelled"]
        # 此前保存的请求、部分回答与事件仍可读取
        messages = await repo.list_messages("c1")
        assert messages[0]["kind"] == "user_request"
        assert [p["text"] for p in await repo.list_partial_answers("r1")] == ["已落盘部分"]


# ---------- 边界补充：NotFound ----------


async def test_operations_on_missing_run_report_not_found(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation("c1")
        # start_run 条件更新不命中 → RunStateConflict；complete_run 目标不存在 → NotFound
        with pytest.raises(RunStateConflict):
            await repo.start_run("missing")
        payload = dump_framework_message(text_response("回答"))
        with pytest.raises(NotFound):
            await repo.complete_run("missing", [("assistant", payload)])
        assert await repo.get_run("missing") is None
        assert await repo.get_run_by_client_request_id("nope") is None
