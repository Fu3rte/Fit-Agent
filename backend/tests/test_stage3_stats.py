"""Stage 3 S3-12：统计确定性计算（``pr_candidates`` 视图、完成率 Wn、三桶、PR 现算）。

验收对照（stage3.md §5 S3-12；06 6.1–6.4 与 06 验收 1–9）：

- 完成率 = 同一计划版本同一 Wn 内「已完成计划训练次数 ÷ 已到期应训练次数」；分母来自已锁定
  应训练日程（漏练保留、未到期与锁定前取消的名额不在内），分母为零显示「暂无」（验收 1–3）。
- 三桶判定顺序「已知未满足优先」，次数与 RIR 按显式闭区间（含端点、无隐藏容差、单值同值上下
  界）；工作台与复盘共用同一确定性结果（验收 4–5）。
- PR 经 ``pr_candidates`` 视图现算：只取当前有效修订，排除待补全／作废／旧修订／回归期／热身／
  人工辅助；缺 RIR 不阻止已确认重量与次数参与；同重量按单组最高次数、不累计多组；更正与作废
  后立即重算（验收 6–9）。

边界：领域规则表驱动用例不碰库；集成用例全部经内部应用层（``RecordDraftService``／
``ArrangementDraftService``／``ConfirmService``）与 ``tmp_path`` 临时文件库，正式事实只经 S3-11
的确认链路写入，不接 HTTP、不触碰真实用户数据目录。
"""

import shutil
from datetime import date
from pathlib import Path

import pytest

from app.confirm import RecordCommitResult
from domain.plan.repo import PlanRepo, ScheduledSessionRecord
from domain.plan.schema import (
    ArrangementTarget,
    DisplaySnapshot,
    IntRange,
    PlanExerciseItem,
    Progression,
    RepsPrescription,
    TimedPrescription,
)
from domain.records.schema import DraftExerciseLog, ExerciseLogFacts, SetFacts
from domain.stats.rules import (
    BUCKET_FIT,
    BUCKET_INCOMPLETE,
    BUCKET_UNMET,
    judge_target_sets,
    judge_work_set,
    plan_week_bounds,
)
from domain.stats.schema import BucketCounts
from domain.stats.service import StatsService
from storage.db import Database
from storage.migrations import DEFAULT_MIGRATIONS_DIR, load_migrations
from tests.support import open_database
from tests.test_stage3_arrangement_confirm import (
    _confirm_arrangement,
    _create_arrangement,
    _mark_cancelled,
    _profile_and_plan,
)
from tests.test_stage3_plan_drafts import REVIEW_ON, STARTS_ON
from tests.test_stage3_record_confirm import _confirm, _void
from tests.test_stage3_record_drafts import (
    _bodyweight_log,
    _weight_log,
    _weight_set,
)
from tests.test_stage3_record_drafts import (
    _create as _create_record,
)

BENCH_ITEM_KEY = "push-01"  # PPL push 日第 1 项：平板杠铃卧推（处方 6–8 次、RIR 1–3）
BENCH_EXERCISE_ID = "barbell-bench-press"
SQUAT_EXERCISE_ID = "barbell-back-squat"
BODYWEIGHT_EXERCISE_ID = "pull-up"
BARBELL_TOTAL = "barbell_includes_bar_total"

# PPL 投影（anchor = STARTS_ON = 2026-09-14 周一）：W1 = 14 推 / 16 拉 / 18 腿。
PUSH_ON = date(2026, 9, 14)
PULL_ON = date(2026, 9, 16)
W1_END = date(2026, 9, 20)
EXTRA_ON = date(2026, 9, 19)  # W1 内的额外训练（无安排）
W2_FIRST = date(2026, 9, 21)


# ---------- 领域规则表驱动用的固定样例（不碰库） ----------


def _target(
    *,
    item_key: str = BENCH_ITEM_KEY,
    reps_range: tuple[int, int] = (8, 10),
    target_rir: tuple[int, int] | None = (1, 3),
) -> ArrangementTarget:
    """一份当次安排快照：一个次数型目标项 + 一个计时型目标项（计时型不进三桶）。"""
    return ArrangementTarget(
        scheduled_session_id="session-1",
        plan_version_id="plan-1",
        plan_workout_key="push",
        scheduled_on=PUSH_ON,
        exercises=(
            PlanExerciseItem(
                item_key=item_key,
                exercise_id=BENCH_EXERCISE_ID,
                display_snapshot=DisplaySnapshot(
                    name="平板杠铃卧推",
                    equipment_variant="barbell",
                    load_convention=BARBELL_TOTAL,
                ),
                record_type="external_load_reps",
                prescription=RepsPrescription(
                    work_sets=3,
                    reps_range=IntRange(*reps_range),
                    target_rir=None if target_rir is None else IntRange(*target_rir),
                ),
                load=None,
                progression=Progression(
                    method="double_progression", rule="稳定完成后先加次数再加重量"
                ),
            ),
            PlanExerciseItem(
                item_key="push-04",
                exercise_id="plank",
                display_snapshot=DisplaySnapshot(
                    name="平板支撑",
                    equipment_variant="bodyweight",
                    load_convention=None,
                ),
                record_type="timed",
                prescription=TimedPrescription(
                    work_sets=3, duration_seconds_range=IntRange(30, 60)
                ),
                load=None,
                progression=Progression(
                    method="duration_progression", rule="稳定达到时长上限后增加难度"
                ),
            ),
        ),
    )


def _log(
    *,
    item_key: str | None = BENCH_ITEM_KEY,
    exercise_id: str = BENCH_EXERCISE_ID,
    record_type: str = "reps_weight",
    load_notation: str | None = BARBELL_TOTAL,
    sets: tuple[SetFacts, ...],
) -> DraftExerciseLog:
    """纯规则用例的记录动作事实（不落库）。"""
    return DraftExerciseLog(
        position=1,
        facts=ExerciseLogFacts(
            exercise_id=exercise_id,
            record_type=record_type,  # type: ignore[arg-type]
            load_notation=load_notation,  # type: ignore[arg-type]
            target_item_key=item_key,
        ),
        sets=sets,
    )


def _work(reps: int | None, rir: float | None, *, set_no: int = 1) -> SetFacts:
    return SetFacts(set_no=set_no, set_type="work", reps=reps, rir=rir)


# ---------- 原始读取与集成用例助手 ----------


async def _object_sql(db: Database, *, kind: str, name: str) -> str | None:
    """``sqlite_master`` 里某对象的建表／建视图原文（字面量 SQL，不拼接表名）。"""

    async def op(conn):
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row["sql"])

    return await db.under_lock(op)


async def _table_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


def _stats(db: Database) -> StatsService:
    return StatsService(db)


async def _session_on(
    db: Database, plan_version_id: str, on: date
) -> ScheduledSessionRecord:
    for session in await PlanRepo(db).list_sessions(plan_version_id):
        if session.scheduled_on == on:
            return session
    raise AssertionError(f"计划版本没有该训练日：{plan_version_id} / {on}")


async def _accept(db: Database, *, draft_id: str, session_id: str, **overrides) -> str:
    """创建并确认一条安排草稿，返回执行时依据的安排修订 id。"""
    await _create_arrangement(db, draft_id=draft_id, session_id=session_id, **overrides)
    result = await _confirm_arrangement(db, draft_id=draft_id)
    return result.arrangement_revision_id


async def _confirmed(
    db: Database,
    *,
    draft_id: str,
    occurred_on: date,
    exercises: tuple[DraftExerciseLog, ...],
    training_session_id: str | None = None,
    arrangement_revision_id: str | None = None,
    completion_declared: bool = True,
) -> RecordCommitResult:
    """经 S3-10 草稿入口创建、再经 S3-11 确认链路落一条正式记录修订。"""
    await _create_record(
        db,
        draft_id=draft_id,
        occurred_on=occurred_on,
        exercises=exercises,
        training_session_id=training_session_id,
        arrangement_revision_id=arrangement_revision_id,
        completion_declared=completion_declared,
    )
    return await _confirm(db, draft_id=draft_id)


async def _throwaway_draft(
    db: Database, *, draft_id: str, occurred_on: date, training_session_id: str
) -> None:
    """给既有训练身份准备一条丢弃用草稿（作废入口只作用于绑定既有身份的草稿）。"""
    await _create_record(
        db,
        draft_id=draft_id,
        occurred_on=occurred_on,
        exercises=(_weight_log(),),
        training_session_id=training_session_id,
    )


# ---------- 迁移与视图 ----------


async def test_migration_010_creates_pr_candidates_view_on_catalog_vocabulary(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        assert await db.pragma_value("user_version") == len(load_migrations())
        sql = await _object_sql(db, kind="view", name="pr_candidates")
        assert sql is not None
        # 记录口径取目录词表（S3-09 拍板 A）：照抄已验证基准报告的处方词表会得到空结果
        assert "reps_weight" in sql
        assert "external_load_reps" not in sql
        # 只取当前修订；排除待补全／作废（status）、回归期、热身、人工辅助
        assert "current_revision_id" in sql
        assert "is_return_phase" in sql and "set_type" in sql and "assistance" in sql
        # 视图不是表：统计侧不建结果表、不建 PR 快照表（现算，06 6.3）；复盘表由 011 建（S3-13），
        # 不属本任务，但应断言本视图没把统计结果落成表。
        assert (
            await _table_names(db) & {"pr_candidates", "personal_records", "pr_values"}
            == set()
        )


# ---------- 计划周 Wn（06 6.1、验收 2） ----------


@pytest.mark.parametrize(
    ("starts_on", "week_no", "expected"),
    [
        # 计划从周四开始：W1 = 该周四至下一周三，W2 从下一周四开始（验收 2）
        (date(2026, 9, 17), 1, (date(2026, 9, 17), date(2026, 9, 24))),
        (date(2026, 9, 17), 2, (date(2026, 9, 24), date(2026, 10, 1))),
        # 按版本开始日期派生，不吸附自然周
        (STARTS_ON, 1, (date(2026, 9, 14), W2_FIRST)),
        (STARTS_ON, 2, (W2_FIRST, date(2026, 9, 28))),
        (REVIEW_ON, 1, (REVIEW_ON, date(2026, 10, 19))),
    ],
)
def test_plan_week_bounds_table(
    starts_on: date, week_no: int, expected: tuple[date, date]
) -> None:
    assert plan_week_bounds(starts_on, week_no) == expected


@pytest.mark.parametrize("week_no", [0, -1, 1.5, True])
def test_plan_week_bounds_rejects_non_positive_or_non_integer(week_no: object) -> None:
    with pytest.raises(ValueError):
        plan_week_bounds(STARTS_ON, week_no)  # type: ignore[arg-type]


# ---------- 组级三桶（06 6.2、验收 4–5） ----------


REPS_8_10 = IntRange(8, 10)
RIR_1_3 = IntRange(1, 3)


@pytest.mark.parametrize(
    ("reps", "rir", "reps_range", "target_rir", "expected"),
    [
        # 验收 4：目标 8–10 次、RIR 1–3
        (9, 2, REPS_8_10, RIR_1_3, BUCKET_FIT),
        (
            6,
            None,
            REPS_8_10,
            RIR_1_3,
            BUCKET_UNMET,
        ),  # 已知未满足优先，缺 RIR 不改变结论
        (9, None, REPS_8_10, RIR_1_3, BUCKET_INCOMPLETE),
        # 验收 5：闭区间含端点、无隐藏容差、小数不取整
        (8, 1, REPS_8_10, RIR_1_3, BUCKET_FIT),
        (10, 3, REPS_8_10, RIR_1_3, BUCKET_FIT),
        (9, 1.5, REPS_8_10, RIR_1_3, BUCKET_FIT),
        (9, 3.5, REPS_8_10, RIR_1_3, BUCKET_UNMET),
        (11, 2, REPS_8_10, RIR_1_3, BUCKET_UNMET),  # 超过上界不算符合
        (7, 2, REPS_8_10, RIR_1_3, BUCKET_UNMET),
        (None, 2, REPS_8_10, RIR_1_3, BUCKET_INCOMPLETE),
        (None, None, REPS_8_10, RIR_1_3, BUCKET_INCOMPLETE),
        # 处方没有 RIR 目标：RIR 不是必要判定事实
        (9, None, REPS_8_10, None, BUCKET_FIT),
        (6, None, REPS_8_10, None, BUCKET_UNMET),
        (None, 3, REPS_8_10, None, BUCKET_INCOMPLETE),
        # 单值 RIR 按同值上下界处理
        (9, 2, REPS_8_10, IntRange(2, 2), BUCKET_FIT),
        (9, 2.5, REPS_8_10, IntRange(2, 2), BUCKET_UNMET),
        (9, 1.5, REPS_8_10, IntRange(2, 2), BUCKET_UNMET),
    ],
)
def test_judge_work_set_table(
    reps: int | None,
    rir: float | None,
    reps_range: IntRange,
    target_rir: IntRange | None,
    expected: str,
) -> None:
    assert (
        judge_work_set(reps=reps, rir=rir, reps_range=reps_range, target_rir=target_rir)
        == expected
    )


def test_buckets_known_unmet_first_give_three_counts() -> None:
    """验收 4：三组分别符合目标／未符合／待补全，工作台与复盘同源同一结果。"""
    logs = (
        _log(
            sets=(
                _work(9, 2, set_no=1),
                _work(6, None, set_no=2),
                _work(9, None, set_no=3),
            )
        ),
    )
    assert judge_target_sets(_target(), logs) == BucketCounts(
        unmet=1, incomplete=1, fit=1
    )


def test_only_comparable_work_sets_enter_buckets() -> None:
    """无对照、额外组、热身、组类型未明确与计时型都不进三桶（06 6.2）。"""
    target = _target()
    assert judge_target_sets(target, ()) == BucketCounts()
    assert (
        judge_target_sets(
            target,
            (
                # 热身组不进（未明确组类型的组同样不进，不静默认定工作组）
                _log(sets=(SetFacts(set_no=1, set_type="warmup", reps=9, rir=2),)),
                _log(sets=(SetFacts(set_no=1, set_type=None, reps=9, rir=2),)),
                # 无对照动作与目标项不存在的动作不进
                _log(item_key=None, sets=(_work(9, 2),)),
                _log(item_key="legs-01", sets=(_work(9, 2),)),
                # 计时型处方不适用次数＋RIR 判定
                _log(
                    item_key="push-04",
                    exercise_id="plank",
                    record_type="timed",
                    load_notation=None,
                    sets=(SetFacts(set_no=1, set_type="work", duration_seconds=45),),
                ),
            ),
        )
        == BucketCounts()
    )


# ---------- 完成率：Wn 分母/分子（06 6.1、验收 1–3） ----------


async def test_weekly_completion_denominator_and_numerator_recompute(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        push = await _session_on(db, plan.id, PUSH_ON)
        pull = await _session_on(db, plan.id, PULL_ON)

        # 当次安排：推日按计划目标照抄，拉日同样不加调整
        link_push = await _accept(
            db,
            draft_id="arr-push",
            session_id=push.id,
            work_sets=None,
            target_rir=None,
            reason=None,
        )
        link_pull = await _accept(
            db,
            draft_id="arr-pull",
            session_id=pull.id,
            work_sets=None,
            target_rir=None,
            reason=None,
        )
        pull_arrangement = await PlanRepo(db).read_latest_arrangement(pull.id)
        assert pull_arrangement is not None
        pull_item = pull_arrangement.target.exercises[0]

        await _confirmed(
            db,
            draft_id="record-push",
            occurred_on=PUSH_ON,
            exercises=(
                _weight_log(
                    exercise_id=BENCH_EXERCISE_ID, target_item_key=BENCH_ITEM_KEY
                ),
            ),
            arrangement_revision_id=link_push,
        )
        await _confirmed(
            db,
            draft_id="record-pull",
            occurred_on=PULL_ON,
            exercises=(
                _weight_log(
                    exercise_id=pull_item.exercise_id,
                    target_item_key=pull_item.item_key,
                ),
            ),
            arrangement_revision_id=link_pull,
        )
        # 额外训练（无安排关联）：不进分子，也不改变漏练与分母（验收 1）
        await _confirmed(
            db,
            draft_id="record-extra",
            occurred_on=EXTRA_ON,
            exercises=(_weight_log(),),
        )

        stats = _stats(db)
        # (业务日期, 周序号) → 分子／分母；None = 暂无（分母为零）
        # 验收 1：W1 到期 3 次、确认完成 2 次、漏练 1 次、额外训练 1 次 → 2/3
        # 验收 3：无打卡的到期安排保留分母
        cases = [
            (date(2026, 9, 14), 1, (1, 1)),  # 只有当天到期
            (date(2026, 9, 17), 1, (2, 2)),  # 16 日也到期
            (W1_END, 1, (2, 3)),  # 漏练的 18 日保留在分母
            (W2_FIRST, 2, (0, 1)),  # 新周只有 21 日到期，尚无记录
            (W1_END, 2, None),  # 验收 2：W2 全部未到期 → 暂无，不是 0%
            (W1_END, 9, None),  # 版本窗口内没有该周 → 暂无
        ]
        for business_date, week_no, expected in cases:
            result = await stats.weekly_completion(
                plan.id, week_no, business_date=business_date
            )
            if expected is None:
                assert result is None, (business_date, week_no)
                continue
            assert result is not None, (business_date, week_no)
            assert (result.numerator, result.denominator) == expected
            assert (result.week_start, result.week_end) == plan_week_bounds(
                STARTS_ON, week_no
            )

        # 同一次安排无论反馈几次最多贡献一次完成（验收 3）：同一安排上第二条记录不改变分子
        await _confirmed(
            db,
            draft_id="record-push-again",
            occurred_on=PUSH_ON,
            exercises=(
                _weight_log(
                    exercise_id=BENCH_EXERCISE_ID, target_item_key=BENCH_ITEM_KEY
                ),
            ),
            arrangement_revision_id=link_push,
        )
        again = await stats.weekly_completion(plan.id, 1, business_date=W1_END)
        assert again is not None
        assert (again.numerator, again.denominator) == (2, 3)

        # 作废后立即重算（06 6.4）：分子少一次，漏练仍在分母
        await _throwaway_draft(
            db,
            draft_id="record-pull-void",
            occurred_on=PULL_ON,
            training_session_id=(await _pull_identity(db, pull_item.item_key)),
        )
        await _void(db, draft_id="record-pull-void")
        voided = await stats.weekly_completion(plan.id, 1, business_date=W1_END)
        assert voided is not None
        assert (voided.numerator, voided.denominator) == (1, 3)

        # 锁定前取消的名额不再是应训练义务，不进分母（04 4.2）
        await _mark_cancelled(db, push.id)
        cancelled = await stats.weekly_completion(plan.id, 1, business_date=W1_END)
        assert cancelled is not None
        assert (cancelled.numerator, cancelled.denominator) == (0, 2)


async def _pull_identity(db: Database, item_key: str) -> str:
    """拉日已完成记录的训练身份（作废入口要绑定既有身份，不按日期推断）。"""

    async def op(conn):
        async with conn.execute(
            "SELECT DISTINCT r.session_id FROM session_revisions r"
            " JOIN exercise_logs e ON e.session_revision_id = r.id"
            " WHERE e.target_item_key = ? AND r.status = 'valid'",
            (item_key,),
        ) as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 1, rows
        return str(rows[0]["session_id"])

    return await db.under_lock(op)


# ---------- 三桶与 PR 的集成链路（验收 4、6） ----------


async def test_confirmed_record_buckets_and_pr_survive_missing_rir(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        push = await _session_on(db, plan.id, PUSH_ON)
        link = await _accept(
            db,
            draft_id="arr-bench",
            session_id=push.id,
            work_sets=None,
            target_rir=None,
            reason=None,
        )
        result = await _confirmed(
            db,
            draft_id="record-bench",
            occurred_on=PUSH_ON,
            exercises=(
                _weight_log(
                    exercise_id=BENCH_EXERCISE_ID,
                    target_item_key=BENCH_ITEM_KEY,
                    sets=(
                        _weight_set(
                            set_no=1, value="60", reps=7, rir=2, assistance="none"
                        ),
                        _weight_set(set_no=2, value="60", reps=5, rir=None),
                        _weight_set(set_no=3, value="60", reps=7, rir=None),
                    ),
                ),
            ),
            arrangement_revision_id=link,
        )
        assert result.status == "valid"

        stats = _stats(db)
        judged = await stats.judge_session(result.training_session_id)
        assert judged is not None
        assert judged.has_comparison is True
        assert judged.is_return_phase is False
        # PPL 卧推处方 6–8 次、RIR 1–3：一组符合、一组已知未满足、一组缺 RIR 待补全
        assert judged.counts == BucketCounts(unmet=1, incomplete=1, fit=1)
        # 验收 6：组级判定可以待补全，已确认的重量与次数仍参与 PR
        assert (
            await stats.pr_max_load(
                exercise_id=BENCH_EXERCISE_ID, load_notation=BARBELL_TOTAL
            )
            == 60000
        )
        assert (
            await stats.pr_max_reps_at_load(
                exercise_id=BENCH_EXERCISE_ID,
                load_notation=BARBELL_TOTAL,
                load_kg_key=60000,
            )
            == 7
        )


async def test_incomplete_record_joins_pr_only_after_completion_is_confirmed(
    tmp_path: Path,
) -> None:
    """验收 6：整条待补全记录不进 PR；补全并确认后自动参与，无需另说「纳入 PR」。"""
    async with open_database(tmp_path / "app.db") as db:
        await _profile_and_plan(db)

        first = await _confirmed(
            db,
            draft_id="record-light",
            occurred_on=PUSH_ON,
            exercises=(
                _weight_log(
                    sets=(_weight_set(value="60", reps=8),),
                ),
            ),
        )
        assert first.status == "valid"
        pending = await _confirmed(
            db,
            draft_id="record-heavy",
            occurred_on=PULL_ON,
            exercises=(_weight_log(sets=(_weight_set(value="100", reps=None),)),),
        )
        assert (
            pending.status == "incomplete"
        )  # 次数未明确：待补全，允许确认承载已知事实

        stats = _stats(db)
        assert (
            await stats.pr_max_load(
                exercise_id=SQUAT_EXERCISE_ID, load_notation=BARBELL_TOTAL
            )
            == 60000
        )

        # 补全同一身份的次数（同一安排关联不变）后确认：自动参与 PR
        await _confirmed(
            db,
            draft_id="record-heavy-corrected",
            occurred_on=PULL_ON,
            exercises=(_weight_log(sets=(_weight_set(value="100", reps=5),)),),
            training_session_id=pending.training_session_id,
        )
        assert (
            await stats.pr_max_load(
                exercise_id=SQUAT_EXERCISE_ID, load_notation=BARBELL_TOTAL
            )
            == 100000
        )


# ---------- PR 排除集、同重量一次、更正与作废（验收 6–9） ----------


async def test_pr_excludes_warmup_assisted_return_phase_and_bodyweight(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _profile_and_plan(db)

        # 唯一有效独立组：60kg×8 与 60kg×10 同重量两组（验收 7：同重量 PR 取单组最高次数）
        await _confirmed(
            db,
            draft_id="record-ok",
            occurred_on=PUSH_ON,
            exercises=(
                _weight_log(
                    sets=(
                        _weight_set(set_no=1, value="60", reps=8, assistance="none"),
                        _weight_set(set_no=2, value="60", reps=10, assistance="none"),
                    ),
                ),
            ),
        )
        # 热身组（保留原文，不进精确组次统计）
        await _confirmed(
            db,
            draft_id="record-warmup",
            occurred_on=PUSH_ON,
            exercises=(
                _weight_log(
                    sets=(_weight_set(value="140", reps=5, set_type="warmup"),)
                ),
            ),
        )
        # 人工辅助组（实际帮助完成）
        await _confirmed(
            db,
            draft_id="record-assisted",
            occurred_on=PUSH_ON,
            exercises=(
                _weight_log(
                    sets=(
                        _weight_set(
                            value="110", reps=3, assistance="assisted", assisted_reps=2
                        ),
                    )
                ),
            ),
        )
        # 回归期训练：不计入 PR 类统计（06 6.4）
        await _create_record(
            db,
            draft_id="record-return",
            occurred_on=PUSH_ON,
            exercises=(_weight_log(sets=(_weight_set(value="200", reps=5),)),),
            is_return_phase=True,
        )
        await _confirm(db, draft_id="record-return")
        # 自重动作不套用重量 PR
        await _confirmed(
            db,
            draft_id="record-bodyweight",
            occurred_on=PUSH_ON,
            exercises=(
                _bodyweight_log(sets=(SetFacts(set_no=1, set_type="work", reps=20),)),
            ),
        )

        stats = _stats(db)
        assert (
            await stats.pr_max_load(
                exercise_id=SQUAT_EXERCISE_ID, load_notation=BARBELL_TOTAL
            )
            == 60000
        )
        assert (
            await stats.pr_max_reps_at_load(
                exercise_id=SQUAT_EXERCISE_ID,
                load_notation=BARBELL_TOTAL,
                load_kg_key=60000,
            )
            == 10  # 不是 8+10=18
        )
        assert (
            await stats.pr_max_load(
                exercise_id=BODYWEIGHT_EXERCISE_ID, load_notation=BARBELL_TOTAL
            )
            is None
        )
        # 未知动作身份同样无候选，不返回 0
        assert (
            await stats.pr_max_load(
                exercise_id="unknown-exercise", load_notation=BARBELL_TOTAL
            )
            is None
        )


async def test_pr_recomputes_after_correction_void_and_assistance_change(
    tmp_path: Path,
) -> None:
    """验收 8–9：更正不再使用旧值、作废后不参与、仅旁边保护可更新 PR。"""
    async with open_database(tmp_path / "app.db") as db:
        await _profile_and_plan(db)
        stats = _stats(db)

        async def max_load() -> int | None:
            return await stats.pr_max_load(
                exercise_id=SQUAT_EXERCISE_ID, load_notation=BARBELL_TOTAL
            )

        # 验收 8 前半：80kg 更正为 60kg 后不再使用 80kg
        first = await _confirmed(
            db,
            draft_id="record-80",
            occurred_on=PUSH_ON,
            exercises=(_weight_log(sets=(_weight_set(value="80", reps=5),)),),
        )
        assert await max_load() == 80000
        await _confirmed(
            db,
            draft_id="record-60",
            occurred_on=PUSH_ON,
            exercises=(_weight_log(sets=(_weight_set(value="60", reps=5),)),),
            training_session_id=first.training_session_id,
        )
        assert await max_load() == 60000

        # 验收 8 后半：确认作废后不参与统计，旧修订不重复计入
        heavy = await _confirmed(
            db,
            draft_id="record-100",
            occurred_on=PULL_ON,
            exercises=(
                _weight_log(
                    sets=(_weight_set(value="100", reps=5, assistance="none"),)
                ),
            ),
        )
        assert await max_load() == 100000
        await _throwaway_draft(
            db,
            draft_id="record-100-void",
            occurred_on=PULL_ON,
            training_session_id=heavy.training_session_id,
        )
        await _void(db, draft_id="record-100-void")
        assert await max_load() == 60000

        # 验收 9：他人帮助完成的 110kg 不进 PR，更正为「仅旁边保护」后参与
        assisted = await _confirmed(
            db,
            draft_id="record-110",
            occurred_on=date(2026, 9, 21),
            exercises=(
                _weight_log(
                    sets=(
                        _weight_set(
                            value="110",
                            reps=3,
                            assistance="assisted",
                            assisted_reps=3,
                        ),
                    )
                ),
            ),
        )
        assert await max_load() == 60000
        await _confirmed(
            db,
            draft_id="record-110-spotter",
            occurred_on=date(2026, 9, 21),
            exercises=(
                _weight_log(
                    sets=(_weight_set(value="110", reps=3, assistance="spotter_only"),)
                ),
            ),
            training_session_id=assisted.training_session_id,
        )
        assert await max_load() == 110000
        assert (
            await stats.pr_max_reps_at_load(
                exercise_id=SQUAT_EXERCISE_ID,
                load_notation=BARBELL_TOTAL,
                load_kg_key=110000,
            )
            == 3
        )


# ---------- 无对照与作废：不作组级判定 ----------


async def test_judgement_without_comparison_identity_or_after_void(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _profile_and_plan(db)
        stats = _stats(db)

        assert await stats.judge_session("missing-session") is None

        result = await _confirmed(
            db,
            draft_id="record-no-arrangement",
            occurred_on=EXTRA_ON,
            exercises=(_weight_log(sets=(_weight_set(value="60", reps=8),)),),
        )
        judged = await stats.judge_session(result.training_session_id)
        assert judged is not None
        assert judged.has_comparison is False  # 无对照不判定，不要求补造目标
        assert judged.counts == BucketCounts()

        await _throwaway_draft(
            db,
            draft_id="record-no-arrangement-void",
            occurred_on=EXTRA_ON,
            training_session_id=result.training_session_id,
        )
        await _void(db, draft_id="record-no-arrangement-void")
        assert await stats.judge_session(result.training_session_id) is None


# ---------- 损坏数据：assisted_reps 有值但 assistance 未标记仍不得进 PR（06 6.3 验收 9） ----------


async def test_pr_view_excludes_assisted_reps_even_when_assistance_unmarked(
    tmp_path: Path,
) -> None:
    """``assisted_reps`` 有值而 ``assistance`` 未标记的行（历史／损坏态）不得进 PR。

    生产校验（``domain/records/rules._validate_set``）已拒该组合，故这里用原始 UPDATE 造出
    「阶段外损坏态替身」，验证视图侧（迁移 012）独立兜底：只看 ``assistance`` 会把这类组当
    独立完成计入（S3-12 残留⑦）。
    """
    async with open_database(tmp_path / "app.db") as db:
        await _profile_and_plan(db)
        await _confirmed(
            db,
            draft_id="record-assist-corrupt",
            occurred_on=PUSH_ON,
            exercises=(
                _weight_log(sets=(_weight_set(value="120", reps=4, assistance=None),)),
            ),
        )
        stats = _stats(db)
        assert (
            await stats.pr_max_load(
                exercise_id="barbell-back-squat", load_notation=BARBELL_TOTAL
            )
            == 120000
        )

        # 损坏态替身：assisted_reps 有值、assistance 仍为 NULL（绕过校验的原始写入）
        async with db.transaction() as conn:
            await conn.execute("UPDATE training_sets SET assisted_reps = 2")

        assert (
            await stats.pr_max_load(
                exercise_id="barbell-back-squat", load_notation=BARBELL_TOTAL
            )
            is None
        )
        assert (
            await stats.pr_max_reps_at_load(
                exercise_id="barbell-back-squat",
                load_notation=BARBELL_TOTAL,
                load_kg_key=120000,
            )
            is None
        )


async def test_migration_012_upgrades_view_to_exclude_assisted_reps(
    tmp_path: Path,
) -> None:
    """011 → 012 升级：既有库的 ``pr_candidates`` 视图被替换为排除 ``assisted_reps`` 的版本。

    验证按编号迁移增量修正（不要求删库重建）：011 时代的视图不含该过滤，升级后含之。
    """
    directory = tmp_path / "through-11-migrations"
    directory.mkdir()
    for name in sorted(path.name for path in DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if int(name.split("_", 1)[0]) <= 11:
            shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory)
    path = tmp_path / "app.db"

    async with open_database(path, migrations_dir=directory) as db:
        assert await db.pragma_value("user_version") == 11
        before = await _object_sql(db, kind="view", name="pr_candidates")
        assert before is not None and "assisted_reps" not in before

    # 用生产迁移目录重开同一库：012 增量执行，视图被替换
    async with open_database(path) as db:
        assert await db.migrate() == len(load_migrations())
        assert await db.pragma_value("user_version") == len(load_migrations())
        after = await _object_sql(db, kind="view", name="pr_candidates")
        assert after is not None
        assert "assisted_reps IS NULL" in after
        assert "assistance IS NOT 'assisted'" in after
