"""test-plan.md 第 2 节 B 组：修订 —— “历史保留，但只有一个当前事实”。"""

from storage.errors import ValidationError

from .base import StoreTestCase, days, must, plan_payload, record_payload

BENCH = "bench_barbell_flat"
NOTATION = "barbell_total"


def adjust_payload(scheduled_session_id: str, target_sets: int) -> dict:
    """接受当次调整：把该日程的卧推目标改为 target_sets 组。"""
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


def correct_payload_full(
    session_id: str,
    occurred_on: str,
    sets: list[dict],
    arrangement_revision_id: str | None = None,
    completion_declared: bool = True,
) -> dict:
    """更正草稿：完整替换本次训练的动作/组事实（完整修订，非字段差量）。"""
    return {
        "session_id": session_id,
        "occurred_on": occurred_on,
        "arrangement_revision_id": arrangement_revision_id,
        "completion_declared": completion_declared,
        "status": "valid",
        "exercises": [
            {
                "item_key": "push-bench",
                "exercise_id": BENCH,
                "exercise_name": "平板杠铃卧推",
                "record_type": "external_load_reps",
                "load_notation": NOTATION,
                "sets": sets,
            }
        ],
    }


def void_payload(session_id: str, occurred_on: str) -> dict:
    return {"session_id": session_id, "occurred_on": occurred_on}


async def setup_plan(store, plan_id="p1", work_sets=3, offsets=(0, 3), starts_in=-7):
    """确认一个 PPL 计划，返回 {plan_id, session_ids, a1_ids}。"""
    p = plan_payload(plan_id, offsets=offsets, starts_in=starts_in, work_sets=work_sets)
    await store.create_draft(plan_id, "create_plan", p)
    res = await store.commit_draft(plan_id, draft_revision=0)
    assert res["committed"]
    session_ids = {
        int(sid.rsplit("-o", 1)[1]): sid for sid in res["result"]["session_ids"]
    }
    a1_ids = {
        int(aid.split(":push:w1-o")[1].split(":")[0]): aid
        for aid in res["result"]["arrangement_ids"]
    }
    return {"plan_id": plan_id, "session_ids": session_ids, "a1_ids": a1_ids}


def bench_work_set(
    load_value: str,
    reps: int = 8,
    rir: int | None = 2,
    set_key: str = "push-bench-1",
    assistance: str = "none",
) -> dict:
    return {
        "set_type": "work",
        "target_set_key": set_key,
        "load": {"value": load_value, "unit": "kg"},
        "reps": reps,
        "rir": rir,
        "assistance": assistance,
    }


async def revision_sets(store, revision_id: str) -> list[dict]:
    """读取指定修订的逐组事实（旧修订不可由公开查询直接读，用只读探针）。"""
    rows = await store._fetchall(
        "SELECT e.position, e.load_notation, t.set_no, t.set_type, t.target_set_key,"
        " t.load_kg_key, t.reps, t.rir, t.assistance"
        " FROM exercise_logs e JOIN training_sets t ON t.exercise_log_id = e.id"
        " WHERE e.session_revision_id=? ORDER BY e.position, t.set_no",
        (revision_id,),
    )
    return [dict(r) for r in rows]


class TestRevisions(StoreTestCase):
    """B. 修订：证明“历史保留，但只有一个当前事实”。"""

    async def _setup_chain(
        self,
        record_load: str = "80",
        record_reps: int = 8,
        arranged_sets: int = 2,
        plan_sets: int = 3,
        record_sets: int = 1,
    ):
        """计划 plan_sets 组 → 安排减为 arranged_sets 组 → 实际记录 record_load×record_reps。"""
        plan = await setup_plan(self.store, plan_id="p1", work_sets=plan_sets)
        sid = plan["session_ids"][0]
        occ = days(-7)
        # 训练前确认调整（先于实际记录，否则会被“已执行”拦截）
        await self.store.create_draft(
            "d-adjust", "adjust_arrangement", adjust_payload(sid, arranged_sets)
        )
        adj = await self.store.commit_draft("d-adjust", draft_revision=0)
        a2 = adj["result"]["arrangement_revision_id"]
        # 实际完成：record_load kg，record_reps 次；record_sets 控制同一训练身份的实际组数
        actual_sets = [
            bench_work_set(record_load, record_reps, set_key=f"push-bench-{i}")
            for i in range(1, record_sets + 1)
        ]
        rec = record_payload(
            f"{plan['plan_id']}-s1",
            occ,
            arrangement_revision_id=a2,
            sets=actual_sets,
        )
        await self.store.create_draft("d-rec", "record_training", rec)
        rr = await self.store.commit_draft("d-rec", draft_revision=0)
        r1 = rr["result"]["revision_id"]
        return {
            "plan": plan,
            "session_id": sid,
            "a2": a2,
            "session_rec": f"{plan['plan_id']}-s1",
            "occurred_on": occ,
            "r1": r1,
        }

    async def test_correction_switches_current_keeps_history(self):
        """更正 80→60：确认前 80 仍是当前事实；确认后 60 成为当前；旧修订仍可查。"""
        chain = await self._setup_chain(record_load="80")
        sess = chain["session_rec"]
        occ = chain["occurred_on"]
        ctx_before = await self.store.get_context_version()

        # 未确认前：当前仍是 r1（80kg）
        cur0 = must(await self.store.get_current_revision(sess))
        self.assertEqual(cur0["id"], chain["r1"])
        self.assertEqual(cur0["sets"][0]["load_kg_key"], 80000)
        # 记住 r1 的原始确认时间，供更正后比对“未被改写”
        r1_before = must(
            await self.store._fetchone(
                "SELECT confirmed_at FROM session_revisions WHERE id=?",
                (chain["r1"],),
            )
        )
        r1_sets_before = await revision_sets(self.store, chain["r1"])
        self.assertEqual(r1_sets_before[0]["load_kg_key"], 80000)
        self.assertEqual(r1_sets_before[0]["reps"], 8)

        # 构造更正草稿（60kg）但不确认
        await self.store.create_draft(
            "d-cor",
            "correct_training",
            correct_payload_full(sess, occ, [bench_work_set("60")]),
        )
        cur1 = must(await self.store.get_current_revision(sess))
        self.assertEqual(cur1["id"], chain["r1"])  # 80kg 仍是当前事实
        self.assertEqual(cur1["sets"][0]["load_kg_key"], 80000)

        # 确认更正 → r2 成为当前
        res = await self.store.commit_draft("d-cor", draft_revision=0)
        self.assertTrue(res["committed"])
        cur2 = must(await self.store.get_current_revision(sess))
        self.assertNotEqual(cur2["id"], chain["r1"])
        self.assertEqual(cur2["sets"][0]["load_kg_key"], 60000)
        # 关联安排仍保留（更正完整修订继承原链接）
        self.assertEqual(cur2["arrangement_revision_id"], chain["a2"])
        self.assertEqual(await self.store.get_context_version(), ctx_before + 1)

        # 旧修订 r1（80kg、原确认时间、来源草稿）仍可查且未被改写
        revs = await self.store.get_session_revisions(sess)
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[0]["id"], chain["r1"])
        self.assertEqual(revs[0]["status"], "valid")
        self.assertEqual(revs[0]["source_draft_id"], "d-rec")
        self.assertIsNotNone(revs[0]["confirmed_at"])
        self.assertEqual(revs[1]["source_draft_id"], "d-cor")
        # r1 的确认时间未被更正改写
        r1_after = must(
            await self.store._fetchone(
                "SELECT confirmed_at FROM session_revisions WHERE id=?",
                (chain["r1"],),
            )
        )
        self.assertEqual(r1_after["confirmed_at"], r1_before["confirmed_at"])
        # r1 的逐组事实仍是 80kg×8（旧修订完整保留）
        r1_sets_after = await revision_sets(self.store, chain["r1"])
        self.assertEqual(r1_sets_after, r1_sets_before)
        self.assertEqual(r1_sets_after[0]["load_kg_key"], 80000)
        self.assertEqual(r1_sets_after[0]["reps"], 8)

        # 训练身份不变：training_sessions 行数不增加
        rows = await self.store._fetchall("SELECT id FROM training_sessions")
        self.assertEqual(len(rows), 1)

        # 当前统计不再使用旧修订的 80kg
        self.assertEqual(await self.store.pr_max_load(BENCH, NOTATION), 60000)

    async def test_correction_without_arrangement_inherits_link(self):
        """更正 payload 未给 arrangement 时，完整修订继承被替换修订的安排链接。"""
        chain = await self._setup_chain(record_load="80")
        sess = chain["session_rec"]
        occ = chain["occurred_on"]
        # 不传 arrangement_revision_id → store 应继承 r1 的 a2
        await self.store.create_draft(
            "d-cor2",
            "correct_training",
            correct_payload_full(
                sess, occ, [bench_work_set("60")], arrangement_revision_id=None
            ),
        )
        await self.store.commit_draft("d-cor2", draft_revision=0)
        cur = must(await self.store.get_current_revision(sess))
        self.assertEqual(cur["arrangement_revision_id"], chain["a2"])
        # 原修订的链接也没有被改写
        revs = await self.store.get_session_revisions(sess)
        r1row = must(
            await self.store._fetchone(
                "SELECT arrangement_revision_id FROM session_revisions WHERE id=?",
                (revs[0]["id"],),
            )
        )
        self.assertEqual(r1row["arrangement_revision_id"], chain["a2"])

    async def test_failed_correction_rolls_back_keeps_old_valid(self):
        """更正事务注入异常回滚 → 旧正式版本仍有效（当前仍 80kg）。"""
        chain = await self._setup_chain(record_load="80")
        sess = chain["session_rec"]
        occ = chain["occurred_on"]
        ctx_before = await self.store.get_context_version()

        await self.store.create_draft(
            "d-cor-fail",
            "correct_training",
            correct_payload_full(sess, occ, [bench_work_set("60")]),
        )
        self.store.fail_after_handler = lambda: (_ for _ in ()).throw(
            RuntimeError("injected crash after correction write")
        )
        with self.assertRaises(RuntimeError):
            await self.store.commit_draft("d-cor-fail", draft_revision=0)
        self.store.fail_after_handler = None

        # 旧正式版本仍有效且是当前事实
        cur = must(await self.store.get_current_revision(sess))
        self.assertEqual(cur["id"], chain["r1"])
        self.assertEqual(cur["sets"][0]["load_kg_key"], 80000)
        self.assertEqual(len(await self.store.get_session_revisions(sess)), 1)
        self.assertEqual(await self.store.pr_max_load(BENCH, NOTATION), 80000)
        self.assertEqual(await self.store.get_context_version(), ctx_before)
        draft_row = must(await self.store.read_draft("d-cor-fail"))
        self.assertEqual(draft_row["status"], "pending")

        # 故障清除后可重试成功
        res = await self.store.commit_draft("d-cor-fail", draft_revision=0)
        self.assertTrue(res["committed"])
        cur2 = must(await self.store.get_current_revision(sess))
        self.assertEqual(cur2["sets"][0]["load_kg_key"], 60000)

    async def test_validation_failure_keeps_old_current(self):
        """更正草稿本身非法（非法 assistance）→ 不产生修订，旧版本仍有效。"""
        chain = await self._setup_chain(record_load="80")
        sess = chain["session_rec"]
        occ = chain["occurred_on"]
        bad_set = bench_work_set("60", assistance="weird_helper")
        await self.store.create_draft(
            "d-cor-bad",
            "correct_training",
            correct_payload_full(sess, occ, [bad_set]),
        )
        with self.assertRaises(ValidationError):
            await self.store.commit_draft("d-cor-bad", draft_revision=0)
        cur = must(await self.store.get_current_revision(sess))
        self.assertEqual(cur["sets"][0]["load_kg_key"], 80000)
        self.assertEqual(len(await self.store.get_session_revisions(sess)), 1)

    async def test_void_excludes_from_stats_no_revert(self):
        """整次作废 → voided 成为当前修订，不参与统计；不退回旧有效版本。"""
        chain = await self._setup_chain(record_load="80")
        sess = chain["session_rec"]
        self.assertEqual(await self.store.pr_max_load(BENCH, NOTATION), 80000)

        await self.store.create_draft(
            "d-void", "void_training", void_payload(sess, chain["occurred_on"])
        )
        res = await self.store.commit_draft("d-void", draft_revision=0)
        self.assertTrue(res["committed"])
        self.assertEqual(res["result"]["status"], "voided")

        cur = must(await self.store.get_current_revision(sess))
        self.assertEqual(cur["status"], "voided")
        self.assertEqual(cur["id"], res["result"]["revision_id"])
        # 作废后不参与 PR
        self.assertIsNone(await self.store.pr_max_load(BENCH, NOTATION))
        # 训练身份仍只有一条（不新增训练次数）
        rows = await self.store._fetchall("SELECT id FROM training_sessions")
        self.assertEqual(len(rows), 1)
        # 不能回退到旧有效版本：无回退接口，当前仍是 voided 修订
        revs = await self.store.get_session_revisions(sess)
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[-1]["status"], "voided")
        # 旧 valid 修订仍保留为历史，但不可再被统计当作当前
        r1 = must(
            await self.store._fetchone(
                "SELECT id, status FROM session_revisions WHERE id=?", (chain["r1"],)
            )
        )
        self.assertEqual(r1["status"], "valid")

    async def test_new_plan_4_sets_keeps_historical_chain(self):
        """长期计划后改为 4 组 → 历史仍还原：原计划 3 组 / 已接受安排 2 组 / 实际 2 组。"""
        # 同一训练身份一次记录两组实际（60kg×8 ×2），对照已接受的 2 组安排
        chain = await self._setup_chain(record_load="60", record_reps=8, record_sets=2)
        sess = chain["session_rec"]
        sid = chain["session_id"]
        occ = chain["occurred_on"]
        # 训练前已确认：该次实际确实包含两组工作组
        cur_before = must(await self.store.get_current_revision(sess))
        self.assertEqual(len(cur_before["sets"]), 2)
        self.assertEqual(cur_before["arrangement_revision_id"], chain["a2"])

        # 长期计划改为 4 组：确认新 plan p2
        p2 = plan_payload("p2", offsets=(0, 3), starts_in=0, work_sets=4)
        await self.store.create_draft("d-p2", "create_plan", p2)
        res2 = await self.store.commit_draft("d-p2", draft_revision=0)
        self.assertTrue(res2["committed"])
        cur_plan = must(
            await self.store._fetchone(
                "SELECT current_plan_version_id FROM user_profile WHERE id=1"
            )
        )
        self.assertEqual(cur_plan["current_plan_version_id"], "p2")

        # 原计划 payload：仍是 3 组（不可原地改写）
        p1 = must(await self.store.get_plan("p1"))
        bench_ex = p1["days"][0]["exercises"][0]
        self.assertEqual(len(bench_ex["sets"]), 3)
        # 该次安排历史：a1 三组 → a2 两组（快照不可变）
        hist = await self.store.get_arrangement_history(sid)
        self.assertEqual(len(hist), 2)
        snap1 = hist[0]["target_snapshot_json"]
        self.assertEqual(len(json_loads(snap1)["exercises"][0]["sets"]), 3)
        snap2 = hist[1]["target_snapshot_json"]
        self.assertEqual(len(json_loads(snap2)["exercises"][0]["sets"]), 2)
        # 同一训练身份的实际组事实在计划替换后不变：2 组、60kg×8×2、仍指向原安排
        cur_after = must(await self.store.get_current_revision(sess))
        self.assertEqual(len(cur_after["sets"]), 2)
        self.assertEqual([s["load_kg_key"] for s in cur_after["sets"]], [60000, 60000])
        self.assertEqual([s["reps"] for s in cur_after["sets"]], [8, 8])
        self.assertEqual(
            [s["target_set_key"] for s in cur_after["sets"]],
            ["push-bench-1", "push-bench-2"],
        )
        self.assertEqual(cur_after["arrangement_revision_id"], chain["a2"])
        # 训练身份不变：只有一个训练身份，没有被计划替换拆出第二条
        rows = await self.store._fetchall("SELECT id FROM training_sessions")
        self.assertEqual(len(rows), 1)
        # 发生日关联的日程仍是原计划下的那个（未被新计划影响）
        scheds = await self.store._fetchall(
            "SELECT plan_version_id, scheduled_on FROM scheduled_sessions WHERE id=?",
            (sid,),
        )
        self.assertEqual(len(scheds), 1)
        self.assertEqual(scheds[0]["plan_version_id"], "p1")
        self.assertEqual(scheds[0]["scheduled_on"], occ)


def json_loads(s: str) -> dict:
    import json

    return json.loads(s)
