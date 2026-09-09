"""test-plan.md 第 2 节 A 组：确认 —— “接受即落盘、提交原子且幂等”。"""

from datetime import datetime, timedelta, timezone

from storage.errors import DraftStale, ValidationError

from .base import StoreTestCase, days, must, plan_payload, record_payload, today

BENCH = "bench_barbell_flat"
NOTATION = "barbell_total"


def adjust_payload(scheduled_session_id: str, target_sets: int) -> dict:
    return {
        "scheduled_session_id": scheduled_session_id,
        "target_snapshot": {
            "schema_version": 1,
            "mode": "regular",
            "adjustment_reason": "用户报告睡眠不足，接受减少一组",
            "exercises": [
                {
                    "item_key": "push-bench",
                    "exercise_id": BENCH,
                    "exercise_name": "平板杠铃卧推",
                    "record_type": "external_load_reps",
                    "load_notation": NOTATION,
                    "sets": [
                        {
                            "set_key": f"push-bench-{i}",
                            "reps_range": [8, 10],
                            "rir_range": [1, 3],
                            "load": {"value": "60", "unit": "kg"},
                        }
                        for i in range(1, target_sets + 1)
                    ],
                }
            ],
        },
    }


async def setup_plan(store, plan_id="p1", work_sets=3, offsets=(0, 3), starts_in=-7):
    """确认一个 PPL 计划，返回 {plan_id, session_ids: {offset: sid}, a1_ids: {offset: aid}}。"""
    p = plan_payload(plan_id, offsets=offsets, starts_in=starts_in, work_sets=work_sets)
    await store.create_draft(plan_id, "create_plan", p)
    res = await store.commit_draft(plan_id, draft_revision=0)
    assert res["committed"]
    # 解析 session ids: {plan_id}:push:w1-o{offset}
    session_ids = {
        int(sid.rsplit("-o", 1)[1]): sid for sid in res["result"]["session_ids"]
    }
    a1_ids = {
        int(aid.split(":push:w1-o")[1].split(":")[0]): aid
        for aid in res["result"]["arrangement_ids"]
    }
    return {"plan_id": plan_id, "session_ids": session_ids, "a1_ids": a1_ids}


class TestConfirmation(StoreTestCase):
    """A. 确认：证明“接受即落盘、提交原子且幂等”。"""

    async def test_unconfirmed_adjust_changes_nothing(self):
        """生成减组草稿但不确认 → 正式安排不变、统计不变。"""
        plan = await setup_plan(self.store)
        sid = plan["session_ids"][0]  # 第一个训练日（相对 starts_on）
        before_arr = await self.store.get_arrangement(plan["a1_ids"][0])
        before_stat = await self.store.weekly_completion("p1", 1, as_of=today())
        before_pr = await self.store.pr_max_load(BENCH, NOTATION)
        before_ctx = await self.store.get_context_version()

        await self.store.create_draft(
            "d-adjust", "adjust_arrangement", adjust_payload(sid, 2)
        )
        # 未确认：读库确认没有新增安排修订
        history = await self.store.get_arrangement_history(sid)
        self.assertEqual(len(history), 1)

        after_arr = must(await self.store.get_arrangement(plan["a1_ids"][0]))
        before_arr = must(before_arr)
        after_stat = await self.store.weekly_completion("p1", 1, as_of=today())
        after_pr = await self.store.pr_max_load(BENCH, NOTATION)
        after_ctx = await self.store.get_context_version()
        self.assertEqual(after_arr["accepted_at"], before_arr["accepted_at"])
        self.assertEqual(
            after_arr["exercises"][0]["sets"][:3][0]["set_key"],
            before_arr["exercises"][0]["sets"][0]["set_key"],
        )
        self.assertEqual(len(after_arr["exercises"][0]["sets"]), 3)  # 仍是 3 组
        self.assertEqual(after_stat, before_stat)
        self.assertEqual(after_pr, before_pr)
        self.assertEqual(after_ctx, before_ctx)

    async def test_confirm_adjust_records_snapshot(self):
        """确认从 3 组减为 2 组 → 保存两组目标、真实接受时间、来源草稿；业务版本只加一。"""
        plan = await setup_plan(self.store)
        sid = plan["session_ids"][0]
        ctx_before = await self.store.get_context_version()
        await self.store.create_draft(
            "d-adjust", "adjust_arrangement", adjust_payload(sid, 2)
        )
        res = await self.store.commit_draft("d-adjust", draft_revision=0)
        self.assertTrue(res["committed"])

        a2 = res["result"]["arrangement_revision_id"]
        arr = must(await self.store.get_arrangement(a2))
        # 两组目标
        self.assertEqual(len(arr["exercises"][0]["sets"]), 2)
        # 真实接受时间：带时区，且落在提交时间窗口内（不是占位值）
        tz_before = datetime.now(timezone.utc) - timedelta(seconds=5)
        self.assertIn("accepted_at", arr)
        accepted = datetime.fromisoformat(arr["accepted_at"])
        self.assertIsNotNone(accepted.tzinfo)
        tz_after = datetime.now(timezone.utc) + timedelta(seconds=5)
        self.assertGreaterEqual(accepted, tz_before)
        self.assertLessEqual(accepted, tz_after)
        # 来源草稿（read_draft payload 里含 source_draft_id）
        hist = await self.store.get_arrangement_history(sid)
        self.assertEqual(hist[-1]["source_draft_id"], "d-adjust")
        # 业务版本只加一
        self.assertEqual(await self.store.get_context_version(), ctx_before + 1)
        # 幂等重复确认：不新增安排、不再次加版本
        res2 = await self.store.commit_draft("d-adjust", draft_revision=0)
        self.assertTrue(res2["idempotent"])
        self.assertEqual(res2["result"], res["result"])
        self.assertEqual(len(await self.store.get_arrangement_history(sid)), 2)
        self.assertEqual(await self.store.get_context_version(), ctx_before + 1)

    async def test_duplicate_confirm_returns_same_result(self):
        """重复确认同一草稿 → 返回第一次结果，不新增安排、不再次加版本。"""
        plan = await setup_plan(self.store)
        sid = plan["session_ids"][0]
        ctx_before = await self.store.get_context_version()
        await self.store.create_draft(
            "d-adjust", "adjust_arrangement", adjust_payload(sid, 2)
        )
        first = await self.store.commit_draft("d-adjust", draft_revision=0)
        second = await self.store.commit_draft("d-adjust", draft_revision=0)
        self.assertTrue(first["committed"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["result"], first["result"])
        self.assertEqual(len(await self.store.get_arrangement_history(sid)), 2)
        self.assertEqual(await self.store.get_context_version(), ctx_before + 1)

    async def test_second_draft_same_base_is_stale(self):
        """同一业务版本生成两份草稿，先确认其中一份 → 另一份首次确认报 draft_stale。"""
        plan = await setup_plan(self.store)
        sid = plan["session_ids"][0]
        # 同 base 版本创建两份调整草稿
        await self.store.create_draft(
            "d1", "adjust_arrangement", adjust_payload(sid, 2)
        )
        await self.store.create_draft(
            "d2", "adjust_arrangement", adjust_payload(sid, 1)
        )
        res1 = await self.store.commit_draft("d1", draft_revision=0)
        self.assertTrue(res1["committed"])
        with self.assertRaises(DraftStale):
            await self.store.commit_draft("d2", draft_revision=0)

    async def test_transaction_rollback_leaves_no_partial(self):
        """写入安排后、更新草稿状态前模拟异常 → 整个事务回滚，不留半份正式数据。"""
        plan = await setup_plan(self.store)
        sid = plan["session_ids"][0]
        ctx_before = await self.store.get_context_version()

        def boom():
            raise RuntimeError("injected crash after formal write")

        self.store.fail_after_handler = boom
        await self.store.create_draft(
            "d-crash", "adjust_arrangement", adjust_payload(sid, 2)
        )
        with self.assertRaises(RuntimeError):
            await self.store.commit_draft("d-crash", draft_revision=0)
        self.store.fail_after_handler = None

        # 无半份：安排历史仍 1 条（a1），无 a2，版本未变，草稿仍 pending
        hist = await self.store.get_arrangement_history(sid)
        self.assertEqual(len(hist), 1)
        self.assertEqual(await self.store.get_context_version(), ctx_before)
        draft = must(await self.store.read_draft("d-crash"))
        self.assertEqual(draft["status"], "pending")
        # 崩溃后可重试成功
        res = await self.store.commit_draft("d-crash", draft_revision=0)
        self.assertTrue(res["committed"])
        self.assertEqual(len(await self.store.get_arrangement_history(sid)), 2)

    async def test_retry_after_commit_is_idempotent(self):
        """提交后模拟响应丢失再重试 → 返回已有结果，不重复写入。"""
        plan = await setup_plan(self.store)
        sid = plan["session_ids"][0]
        ctx_before = await self.store.get_context_version()
        await self.store.create_draft(
            "d-lost", "adjust_arrangement", adjust_payload(sid, 2)
        )
        first = await self.store.commit_draft("d-lost", draft_revision=0)
        # 模拟客户端丢响应：再确认一次（同 draft_revision）
        retry = await self.store.commit_draft("d-lost", draft_revision=0)
        self.assertTrue(retry["idempotent"])
        self.assertEqual(retry["result"], first["result"])
        self.assertEqual(len(await self.store.get_arrangement_history(sid)), 2)
        self.assertEqual(await self.store.get_context_version(), ctx_before + 1)

    async def test_restart_preserves_accepted_arrangement(self):
        """关闭数据库连接，重新打开 → 安排仍存在，不需要等打卡才补存。"""
        plan = await setup_plan(self.store)
        sid = plan["session_ids"][0]
        await self.store.create_draft(
            "d-restart", "adjust_arrangement", adjust_payload(sid, 2)
        )
        await self.store.commit_draft("d-restart", draft_revision=0)
        ctx_before = await self.store.get_context_version()

        await self.reopen()

        hist_after = await self.store.get_arrangement_history(sid)
        self.assertEqual(len(hist_after), 2)
        self.assertEqual(hist_after[1]["source_draft_id"], "d-restart")
        self.assertEqual(await self.store.get_context_version(), ctx_before)
        # 无需打卡即存在
        scheds = await self.store._fetchall(
            "SELECT id FROM scheduled_sessions WHERE plan_version_id='p1'"
        )
        self.assertEqual(len(scheds), 2)

    async def test_version_increments_once_per_plan_commit(self):
        """计划确认是一次业务变更，业务版本 +1。"""
        ctx0 = await self.store.get_context_version()
        await setup_plan(self.store)
        self.assertEqual(await self.store.get_context_version(), ctx0 + 1)

    async def test_corrupt_record_rolls_back(self):
        """记录训练时组数据非法（在插入训练身份之后失败）→ 整体回滚不留半份。"""
        plan = await setup_plan(self.store)
        sid = plan["session_ids"][0]
        aid = plan["a1_ids"][0]
        ctx_before = await self.store.get_context_version()
        payload = record_payload(
            sid,
            days(-7),
            arrangement_revision_id=aid,
            sets=[
                {
                    "set_type": "work",
                    "target_set_key": "push-bench-1",
                    "load": {"value": "60", "unit": "kg"},
                    "reps": 8,
                    "rir": 2,
                    "assistance": "weird_helper",
                }
            ],  # 非法 assistance → 校验失败
        )
        await self.store.create_draft("d-bad", "record_training", payload)
        with self.assertRaises(ValidationError):
            await self.store.commit_draft("d-bad", draft_revision=0)
        # 训练身份也没留下（事务回滚）
        rows = await self.store._fetchall("SELECT id FROM training_sessions")
        self.assertEqual(len(rows), 0)
        self.assertEqual(await self.store.get_context_version(), ctx_before)
