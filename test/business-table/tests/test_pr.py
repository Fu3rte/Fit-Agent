"""test-plan.md 第 2 节 D 组：PR —— 证明“资格来自事实，而不是手工标记”。

资格 9 行 + 附加检查全部通过真实业务入口（record/correct/void draft）写入。
所有日期相对 date.today()（不依赖测试当天）。每条用例独立临时库。
"""

from storage.loadkey import load_kg_key

from .base import StoreTestCase, days, must

BENCH = "bench_barbell_flat"
NOTATION = "barbell_total"
DUMBBELL = "db_flat_press"


def work_set(
    *,
    load_value="60",
    unit="kg",
    reps=8,
    rir=None,
    assistance="none",
    target_set_key="push-bench-1",
):
    d = {
        "set_type": "work",
        "target_set_key": target_set_key,
        "load": {"value": load_value, "unit": unit},
        "reps": reps,
    }
    if rir is not None:
        d["rir"] = rir
    d["assistance"] = assistance
    return d


async def commit_record(
    store,
    draft_id,
    session_id,
    *,
    sets,
    occurred_on=None,
    arrangement_revision_id=None,
    completion_declared=True,
    status="valid",
    is_return_phase=False,
    exercise_id=BENCH,
    notation=NOTATION,
):
    """通过 record_training 草稿确认一条正式/待补全训练记录。"""
    payload = {
        "session_id": session_id,
        "occurred_on": occurred_on or days(-1),
        "arrangement_revision_id": arrangement_revision_id,
        "completion_declared": completion_declared,
        "status": status,
        "is_return_phase": is_return_phase,
        "exercises": [
            {
                "item_key": "push-bench",
                "exercise_id": exercise_id,
                "exercise_name": "平板卧推",
                "record_type": "external_load_reps",
                "load_notation": notation,
                "sets": sets,
            }
        ],
    }
    await store.create_draft(draft_id, "record_training", payload)
    res = await store.commit_draft(draft_id, draft_revision=0)
    assert res["committed"]
    return res["result"]


async def commit_correction(store, draft_id, session_id, *, sets, occurred_on=None):
    """通过 correct_training 草稿把整次训练更改为新事实（完整修订）。"""
    payload = {
        "session_id": session_id,
        "occurred_on": occurred_on or days(-1),
        "completion_declared": True,
        "status": "valid",
        "exercises": [
            {
                "item_key": "push-bench",
                "exercise_id": BENCH,
                "exercise_name": "平板卧推",
                "record_type": "external_load_reps",
                "load_notation": NOTATION,
                "sets": sets,
            }
        ],
    }
    await store.create_draft(draft_id, "correct_training", payload)
    res = await store.commit_draft(draft_id, draft_revision=0)
    assert res["committed"]
    return res["result"]


async def void_session(store, draft_id, session_id, occurred_on=None):
    """通过 void_training 草稿作废整次训练。"""
    payload = {"session_id": session_id, "occurred_on": occurred_on or days(-1)}
    await store.create_draft(draft_id, "void_training", payload)
    res = await store.commit_draft(draft_id, draft_revision=0)
    assert res["committed"]
    return res["result"]


class TestPr(StoreTestCase):
    """D. PR：资格来自事实（正式状态/修订/组类型/辅助/阶段/动作口径），不是手工标记。"""

    async def test_eligibility_matrix(self):
        """资格 9 行中的 8 行（草稿行单测）：同动作同口径下只有合格组决定 max_load。

        - 正式独立工作组缺 RIR → 进
        - 整条待补全(incomplete)记录 → 不进
        - 热身组 → 不进
        - 实际有人发力辅助(assisted) → 不进（但保留真实工作组）
        - 仅旁边保护(spotter_only) → 进
        - 回归期(is_return_phase) → 不进
        - 旧修订(更正确认前) → 不进
        - 作废记录 → 不进
        - 无计划关联的正式独立工作组 → 进
        """
        # 无计划关联的正式独立工作组，缺 RIR → 进
        await commit_record(
            self.store,
            "d-no-plan",
            "t-no-plan",
            sets=[work_set(load_value="70", rir=None)],
            completion_declared=True,
        )
        # valid 记录：热身组 90kg + 工作组 60kg → 只进工作组
        await commit_record(
            self.store,
            "d-warm",
            "t-warm",
            sets=[
                {
                    "set_type": "warmup",
                    "load": {"value": "90", "unit": "kg"},
                    "reps": 5,
                },
                work_set(load_value="60", rir=2),
            ],
        )
        # 整条待补全记录：status=incomplete → 不进
        await commit_record(
            self.store,
            "d-incomplete",
            "t-incomplete",
            sets=[work_set(load_value="85", rir=None)],
            status="incomplete",
        )
        # 实际有人发力辅助：正式工作组但排除 PR
        await commit_record(
            self.store,
            "d-assisted",
            "t-assisted",
            sets=[work_set(load_value="95", rir=None, assistance="assisted")],
        )
        # 仅旁边保护、没有接触或帮助 → 进（成为当前 max_load）
        await commit_record(
            self.store,
            "d-spotter",
            "t-spotter",
            sets=[
                work_set(load_value="100", reps=6, rir=None, assistance="spotter_only")
            ],
        )
        # 回归期训练 → 不进
        await commit_record(
            self.store,
            "d-return",
            "t-return",
            sets=[work_set(load_value="105", rir=None)],
            is_return_phase=True,
        )
        # 作废记录：先 valid 后 void → 不进
        await commit_record(
            self.store,
            "d-void-before",
            "t-void",
            sets=[work_set(load_value="110", rir=None)],
        )
        await void_session(self.store, "d-void", "t-void")
        # 旧修订：80kg 被更正确认 → r1 不再进，当前 60kg 进
        await commit_record(
            self.store,
            "d-corr-before",
            "t-corr",
            sets=[work_set(load_value="80", rir=2)],
        )
        await commit_correction(
            self.store, "d-corr", "t-corr", sets=[work_set(load_value="60", rir=2)]
        )

        # 唯一合格最大：spotter_only 100kg
        self.assertEqual(await self.store.pr_max_load(BENCH, NOTATION), 100000)
        # 无计划关联正式组 70kg 确实在候选里
        self.assertEqual(
            await self.store.pr_max_reps_at_load(BENCH, NOTATION, 70000), 8
        )
        # 热身组 90kg 未混入
        self.assertIsNone(await self.store.pr_max_reps_at_load(BENCH, NOTATION, 90000))
        # 待补全整条记录未混入
        self.assertIsNone(await self.store.pr_max_reps_at_load(BENCH, NOTATION, 85000))
        # assisted 组未混入 PR，但作为真实工作组保留在正式修订中
        self.assertIsNone(await self.store.pr_max_reps_at_load(BENCH, NOTATION, 95000))
        cur = must(await self.store.get_current_revision("t-assisted"))
        assisted = [s for s in cur["sets"] if s["load_kg_key"] == 95000]
        self.assertEqual(len(assisted), 1)
        self.assertEqual(assisted[0]["assistance"], "assisted")
        self.assertEqual(assisted[0]["set_type"], "work")
        # 回归期记录未混入
        self.assertIsNone(await self.store.pr_max_reps_at_load(BENCH, NOTATION, 105000))
        # 作废记录未混入，作废修订保留完整事实
        self.assertIsNone(await self.store.pr_max_reps_at_load(BENCH, NOTATION, 110000))
        cur = must(await self.store.get_current_revision("t-void"))
        self.assertEqual(cur["status"], "voided")
        # 旧修订（80kg r1）不再参与：该 load 在候选里应为 None
        self.assertIsNone(await self.store.pr_max_reps_at_load(BENCH, NOTATION, 80000))
        # 更正确认后的 60kg 是当前事实并参与 PR
        self.assertEqual(
            await self.store.pr_max_reps_at_load(BENCH, NOTATION, 60000), 8
        )

    async def test_pending_draft_not_in_pr(self):
        """未确认草稿不产生任何候选；确认前统计不变。"""
        await commit_record(
            self.store, "d-real", "t-real", sets=[work_set(load_value="75", rir=None)]
        )
        ctx_before = await self.store.get_context_version()
        pr_before = await self.store.pr_max_load(BENCH, NOTATION)
        count_before = len(
            await self.store._fetchall("SELECT id FROM training_sessions")
        )
        # 草稿化：只 create 不 commit
        await self.store.create_draft(
            "d-pending",
            "record_training",
            {
                "session_id": "t-pending",
                "occurred_on": days(-1),
                "completion_declared": True,
                "status": "valid",
                "exercises": [
                    {
                        "item_key": "push-bench",
                        "exercise_id": BENCH,
                        "exercise_name": "平板卧推",
                        "record_type": "external_load_reps",
                        "load_notation": NOTATION,
                        "sets": [work_set(load_value="120", rir=None)],
                    }
                ],
            },
        )
        self.assertEqual(await self.store.pr_max_load(BENCH, NOTATION), pr_before)
        self.assertEqual(await self.store.get_context_version(), ctx_before)
        after = await self.store._fetchall("SELECT id FROM training_sessions")
        self.assertEqual(len(after), count_before)

    async def test_max_reps_not_summed(self):
        """同重量两组分别做 8、10 次 → 纪录是 10 次，不是 18 次。"""
        await commit_record(
            self.store,
            "d-same-load",
            "t-same-load",
            sets=[
                work_set(load_value="60", reps=8, rir=2),
                work_set(
                    load_value="60", reps=10, rir=1, target_set_key="push-bench-2"
                ),
            ],
        )
        # 修正 target_set_key 默认值造成的重复 key（同 log 内 set_no 唯一，key 可重复）
        self.assertEqual(
            await self.store.pr_max_reps_at_load(BENCH, NOTATION, 60000), 10
        )

    async def test_correction_drops_max_load(self):
        """80kg 更正为 60kg 且无其他有效 80kg → PR 从 80000 降为 60000，旧修订可追溯。"""
        await commit_record(
            self.store, "d-rec", "t-cor", sets=[work_set(load_value="80", rir=2)]
        )
        self.assertEqual(await self.store.pr_max_load(BENCH, NOTATION), 80000)
        ctx_before = await self.store.get_context_version()

        await commit_correction(
            self.store, "d-cor", "t-cor", sets=[work_set(load_value="60", rir=2)]
        )
        self.assertEqual(await self.store.pr_max_load(BENCH, NOTATION), 60000)
        self.assertIsNone(await self.store.pr_max_reps_at_load(BENCH, NOTATION, 80000))
        # 训练身份不变、修订追加：r1(80kg) 与 r2(60kg) 都保留
        revs = await self.store.get_session_revisions("t-cor")
        self.assertEqual([r["revision_no"] for r in revs], [1, 2])
        self.assertEqual(
            revs[0]["status"], "valid"
        )  # 历史修订仍可查（含确认时间/来源草稿）
        self.assertEqual(revs[0]["source_draft_id"], "d-rec")
        # 单次更正 = 一次业务提交
        self.assertEqual(await self.store.get_context_version(), ctx_before + 1)

    async def test_notation_isolation(self):
        """单只哑铃与双只总重是不同负重口径，各自独立 PR，不能混比。"""
        await commit_record(
            self.store,
            "d-single",
            "t-single",
            sets=[work_set(load_value="30", rir=None)],
            exercise_id=DUMBBELL,
            notation="dumbbell_single",
        )
        await commit_record(
            self.store,
            "d-total",
            "t-total",
            sets=[work_set(load_value="60", rir=None)],
            exercise_id=DUMBBELL,
            notation="dumbbell_total",
        )
        self.assertEqual(
            await self.store.pr_max_load(DUMBBELL, "dumbbell_single"), 30000
        )
        self.assertEqual(
            await self.store.pr_max_load(DUMBBELL, "dumbbell_total"), 60000
        )
        # 单只 30kg 的记录不能在双只口径里被查到（不混比）
        self.assertIsNone(
            await self.store.pr_max_reps_at_load(DUMBBELL, "dumbbell_total", 30000)
        )

    async def test_load_key_conversion_rule(self):
        """kg/lb 换算按选定精度规则：1 lb = 0.45359237 kg，×1000 HALF_EVEN。"""
        k40kg = must(load_kg_key({"value": "40", "unit": "kg"}))
        k88lb = must(load_kg_key({"value": "88", "unit": "lb"}))
        k882lb = must(load_kg_key({"value": "88.2", "unit": "lb"}))
        self.assertEqual(k40kg, 40000)
        self.assertEqual(k88lb, 39916)
        self.assertEqual(k882lb, 40007)
        # 88lb < 40kg < 88.2lb：换算后的排序必须与物理事实一致
        self.assertLess(k88lb, k40kg)
        self.assertLess(k40kg, k882lb)
        # 同一重量用不同单位写法归一到同一 key（等价，无隐藏容差）
        self.assertEqual(
            must(load_kg_key({"value": "45.359237", "unit": "kg"})),
            must(load_kg_key({"value": "100", "unit": "lb"})),
        )
        # 经真实业务入口（create_draft→commit_draft）保存 lb 记录后，PR 排序按换算键比较
        await commit_record(
            self.store,
            "d-lb",
            "t-lb",
            sets=[work_set(load_value="88", unit="lb", reps=6, rir=None)],
        )
        await commit_record(
            self.store,
            "d-kg",
            "t-kg",
            sets=[work_set(load_value="40", unit="kg", reps=8, rir=None)],
        )
        # 88lb(39916) 与 40kg(40000) 是不同键：40kg 更高 → PR max = 40000
        self.assertEqual(await self.store.pr_max_load(BENCH, NOTATION), 40000)
        self.assertEqual(
            await self.store.pr_max_reps_at_load(BENCH, NOTATION, 40000), 8
        )
        self.assertEqual(
            await self.store.pr_max_reps_at_load(BENCH, NOTATION, 39916), 6
        )

    async def test_pr_records_same_across_surfaces(self):
        """工作台与复盘共用同一 pr 结果（同源确定性计算，双入口各自校验具体数值）。"""
        await commit_record(
            self.store, "d-a", "t-a", sets=[work_set(load_value="80", rir=None)]
        )
        await commit_record(
            self.store, "d-b", "t-b", sets=[work_set(load_value="80", reps=9, rir=None)]
        )
        # 工作台口径：查询函数给出 80kg(80000) 单组最高 9 次（不是 17 次）
        workbench = {
            "max_load": await self.store.pr_max_load(BENCH, NOTATION),
            "max_reps_at_load": await self.store.pr_max_reps_at_load(
                BENCH, NOTATION, 80000
            ),
        }
        # 复盘口径：独立入口（review_pr_summary）基于同一 pr_candidates 视图
        review = await self.store.review_pr_summary(BENCH, NOTATION)
        self.assertIsNotNone(review)
        assert review is not None
        self.assertEqual(review["max_load_kg_key"], 80000)
        self.assertEqual(review["max_reps_at_max_load"], 9)
        self.assertEqual(review["max_load_kg_key"], workbench["max_load"])
        self.assertEqual(review["max_reps_at_max_load"], workbench["max_reps_at_load"])
