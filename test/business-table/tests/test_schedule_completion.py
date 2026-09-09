"""test-plan.md §2 C 组：漏练 —— 分母来自应训练日程，不来自打卡。

同一计划版本、同一 W1：已到期应训练三次，正式确认完成两次，第三次无打卡；
另有一次额外训练（无安排关联）与一次未来安排。固定日期 = 固定业务日历。
"""

from storage.errors import ScheduleLocked, ValidationError
from storage.store import BusinessStore

from .base import CLOCK, StoreTestCase, days, plan_payload, record_payload, today

OFFS = (0, 1, 2, 6)  # 前三个 = 已到期（今天-3/-2/-1），off6 = 未来（今天+3）


async def setup_week(store, plan_id="p1"):
    """W1 含 3 个已到期 push 日程 + 1 个未来日程的计划；as_of=今天。"""
    p = plan_payload(plan_id, offsets=OFFS, starts_in=-3, work_sets=3)
    await store.create_draft(plan_id, "create_plan", p)
    res = await store.commit_draft(plan_id, draft_revision=0)
    assert res["committed"]
    ids = res["result"]["session_ids"]
    arr_ids = res["result"]["arrangement_ids"]
    return {
        "plan_id": plan_id,
        "sid": dict(zip(OFFS, ids, strict=True)),  # {offset: scheduled_session_id}
        "aid": dict(
            zip(OFFS, arr_ids, strict=True)
        ),  # {offset: arrangement_revision_id}
    }


async def record_complete(
    store, draft_id, session_id, occurred_on, aid, *, feedback_no=""
):
    """经业务入口确认一条完成训练记录。"""
    payload = record_payload(
        session_id,
        occurred_on,
        arrangement_revision_id=aid,
        completion_declared=True,
        status="valid",
        session_id_suffix=feedback_no,
    )
    await store.create_draft(draft_id, "record_training", payload)
    res = await store.commit_draft(draft_id, draft_revision=0)
    assert res["committed"]
    return res


class TestScheduleCompletion(StoreTestCase):
    """C. 漏练：证明分母来自应训练日程，且锁定规则独立于后台标记。"""

    async def test_due_three_done_two_missed_one(self):
        """到期 3 次、完成 2 次、漏练 1 次（另有额外训练与未来安排）→ 2/3。"""
        week = await setup_week(self.store)
        await record_complete(
            self.store, "d1", week["sid"][0], days(-3), week["aid"][0]
        )
        await record_complete(
            self.store, "d2", week["sid"][1], days(-2), week["aid"][1]
        )
        # 额外训练：无安排关联，即使声明完成也不进分子分母
        await record_complete(self.store, "d-extra", "extra-1", days(0), None)
        stat = await self.store.weekly_completion("p1", 1, as_of=today())
        assert stat is not None
        self.assertEqual(stat["denominator"], 3)  # 未来安排不进分母
        self.assertEqual(stat["numerator"], 2)
        self.assertAlmostEqual(stat["numerator"] / stat["denominator"], 2 / 3, places=3)

    async def test_missed_session_cannot_be_deleted_or_rescheduled(self):
        """漏练后删除或改期原日程 → ScheduleLocked；锁定判定不依赖锁定字段。"""
        week = await setup_week(self.store)
        sid = week["sid"][2]  # 今天-1：已到期、未打卡
        # 服务停机场景：从未写过 denominator_locked_at（无任何训练记录触发补记）
        row = await self.store._fetchone(
            "SELECT scheduled_on, denominator_locked_at FROM scheduled_sessions WHERE id=?",
            (sid,),
        )
        assert row is not None
        self.assertIsNone(row["denominator_locked_at"])
        with self.assertRaises(ScheduleLocked):
            await self.store.delete_scheduled_session(sid)
        with self.assertRaises(ScheduleLocked):
            await self.store.reschedule_session(sid, days(5))
        # 数据未被洗掉
        row2 = await self.store._fetchone(
            "SELECT scheduled_on, cancelled_at FROM scheduled_sessions WHERE id=?",
            (sid,),
        )
        assert row2 is not None
        self.assertEqual(row2["scheduled_on"], row["scheduled_on"])
        self.assertIsNone(row2["cancelled_at"])

    async def test_reschedule_still_locked_after_restart(self):
        """服务停机跨过训练日，重启后再试改期 → 仍被拒绝（日期规则，非仅字段）。"""
        week = await setup_week(self.store)
        await record_complete(
            self.store, "d1", week["sid"][0], days(-3), week["aid"][0]
        )
        await record_complete(
            self.store, "d2", week["sid"][1], days(-2), week["aid"][1]
        )
        await self.reopen()  # 模拟停机重启
        with self.assertRaises(ScheduleLocked):
            await self.store.reschedule_session(week["sid"][2], days(5))
        with self.assertRaises(ScheduleLocked):
            await self.store.delete_scheduled_session(week["sid"][2])

    async def test_makeup_next_day_is_extra_and_miss_remains(self):
        """后一天实际补练 → 额外训练，原漏练与分母不变。"""
        week = await setup_week(self.store)
        await record_complete(
            self.store, "d1", week["sid"][0], days(-3), week["aid"][0]
        )
        await record_complete(
            self.store, "d2", week["sid"][1], days(-2), week["aid"][1]
        )
        # 事后补练：第二天（今天）才练，无安排关联
        await record_complete(self.store, "d-makeup", "makeup-1", days(0), None)
        stat = await self.store.weekly_completion("p1", 1, as_of=today())
        assert stat is not None
        self.assertEqual(stat["numerator"], 2)
        self.assertEqual(stat["denominator"], 3)  # 原漏练仍在分母

        # 补练若错误关联原漏练安排（发生日 ≠ 安排训练日）→ 被拒绝，不能借关联洗掉漏练
        bad_payload = record_payload(
            "makeup-link-bad",
            days(0),  # 实际练于“后一天”
            arrangement_revision_id=week["aid"][2],  # 原漏练安排在 days(-1)
        )
        await self.store.create_draft("d-bad-link", "record_training", bad_payload)
        with self.assertRaises(ValidationError):
            await self.store.commit_draft("d-bad-link", draft_revision=0)
        stat2 = await self.store.weekly_completion("p1", 1, as_of=today())
        assert stat2 is not None
        self.assertEqual((stat2["numerator"], stat2["denominator"]), (2, 3))

    async def test_shutdown_across_training_day_locks_by_date_rule(self):
        """服务停机跨过训练日：停机期间锁字段从未写入，重开后改期仍被拒绝。

        证明锁定判定依赖日期规则本身，而不是依赖后台/落盘时补写的锁字段。
        """
        # 训练日在“明天”：服务停机前它尚未到期，也不可能有任何写入触发补记锁字段
        p = plan_payload("p-future", offsets=(1,), starts_in=0, work_sets=3)
        await self.store.create_draft("p-future", "create_plan", p)
        res = await self.store.commit_draft("p-future", draft_revision=0)
        assert res["committed"]
        sid = res["result"]["session_ids"][0]
        row = await self.store._fetchone(
            "SELECT scheduled_on, denominator_locked_at FROM scheduled_sessions WHERE id=?",
            (sid,),
        )
        assert row is not None
        self.assertEqual(row["scheduled_on"], days(1))  # 训练日在未来
        self.assertIsNone(row["denominator_locked_at"])  # 停机期间无写入

        # 停机跨过训练日：时钟推进到训练日之后，再“重启”
        await self.store.close()
        CLOCK.advance(3)  # 现在业务日期 > 训练日
        self.store = BusinessStore(self.db_path, today_fn=CLOCK)
        await self.store.open()  # 模拟重启，同一固定时钟

        # 训练日已过：即使锁字段仍为空，改期/删除也按日期规则拒绝
        row2 = await self.store._fetchone(
            "SELECT denominator_locked_at FROM scheduled_sessions WHERE id=?", (sid,)
        )
        assert row2 is not None
        self.assertIsNone(row2["denominator_locked_at"])
        with self.assertRaises(ScheduleLocked):
            await self.store.reschedule_session(sid, days(5))
        with self.assertRaises(ScheduleLocked):
            await self.store.delete_scheduled_session(sid)
        # 数据未被洗掉：日程仍在
        keep = await self.store._fetchone(
            "SELECT id, scheduled_on FROM scheduled_sessions WHERE id=?", (sid,)
        )
        assert keep is not None
        self.assertEqual(keep["scheduled_on"], row["scheduled_on"])

    async def test_same_day_actual_completion_backfilled_later(self):
        """当天确实完成、后来才补录 → 可关联原安排，更正漏练为完成。"""
        week = await setup_week(self.store)
        await record_complete(
            self.store, "d1", week["sid"][0], days(-3), week["aid"][0]
        )
        await record_complete(
            self.store, "d2", week["sid"][1], days(-2), week["aid"][1]
        )
        before = await self.store.weekly_completion("p1", 1, as_of=today())
        assert before is not None
        self.assertEqual((before["numerator"], before["denominator"]), (2, 3))
        # 后来补录：occurred_on = 当天（今天-1），关联该日安排并声明完成
        await record_complete(
            self.store, "d-backfill", week["sid"][2], days(-1), week["aid"][2]
        )
        after = await self.store.weekly_completion("p1", 1, as_of=today())
        assert after is not None
        self.assertEqual((after["numerator"], after["denominator"]), (3, 3))

    async def test_multiple_feedbacks_same_arrangement_count_once(self):
        """同一安排提交多次反馈 → 该日程最多贡献一次完成。"""
        week = await setup_week(self.store)
        # 两条独立训练身份都关联 off0 的安排，且都声明完成
        await record_complete(
            self.store,
            "d-fb1",
            week["sid"][0],
            days(-3),
            week["aid"][0],
            feedback_no="-fb1",
        )
        await record_complete(
            self.store,
            "d-fb2",
            week["sid"][0],
            days(-3),
            week["aid"][0],
            feedback_no="-fb2",
        )
        await record_complete(
            self.store, "d1", week["sid"][1], days(-2), week["aid"][1]
        )
        stat = await self.store.weekly_completion("p1", 1, as_of=today())
        assert stat is not None
        # 若多次反馈被重复计数则分子会是 3；正确实现按日程去重 = 2
        self.assertEqual(stat["numerator"], 2)
        self.assertEqual(stat["denominator"], 3)

    async def test_new_plan_without_due_sessions_returns_none(self):
        """新计划版本尚无到期安排 → 返回 None（显示“暂无”），不是 0% 或 100%。"""
        await setup_week(self.store)  # p1 已有数据
        # 新计划 p2 从明天开始，W1 日程全部在未来
        p2 = plan_payload("p2", offsets=(0,), starts_in=+1, work_sets=3)
        await self.store.create_draft("p2", "create_plan", p2)
        res = await self.store.commit_draft("p2", draft_revision=0)
        assert res["committed"]
        stat = await self.store.weekly_completion("p2", 1, as_of=today())
        self.assertIsNone(stat)  # 分母为零 → 暂无
        # 旧计划不受影响
        stat_p1 = await self.store.weekly_completion("p1", 1, as_of=today())
        assert stat_p1 is not None
        self.assertEqual(stat_p1["denominator"], 3)
