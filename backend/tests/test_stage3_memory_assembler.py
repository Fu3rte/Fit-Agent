"""Stage 3 子任务 02：MemoryAssembler 的固定范围装配与有界读取。

依据：``refactor-log/stage3.md`` §4、§2.1／§2.2；讨论总结 §5.2／§7／§14；REFACTOR_PLAN §8.2。

覆盖：输出恰好六类、最近四次训练的有界读取（超过四次取最新四次、同日训练各算一次、组只属于入选训练、
不足四次与空库、不先读全量、非正数 ``limit`` 直接失败）、只有 active 计划进入上下文且不透明 JSON 不被解释、
未建档画像保持缺失、PB 范围（全部已有动作 / 按稳定 ``exercise_id`` 过滤）与类型组成及来源信息原样保留
（不在 Graph 另算）、``trend_summary`` 与 Stats 服务同源且按注入的业务日期复算、修改／删除训练与更新
画像后重新装配立即反映。

计划与日程按要求用直接 SQL 写入（正式创建／激活入口留 Stage 5）；训练一律经
``WorkoutRecordsService`` 走真实写入路径。测试只用 pytest ``tmp_path`` 下的独立临时库。
"""

from dataclasses import fields
from datetime import date
from pathlib import Path

import pytest

from domain.body_metrics.service import BodyMetricsService
from domain.plans.schema import Plan
from domain.plans.service import PlanReadService
from domain.profile.schema import Fact, Profile
from domain.profile.service import ProfileService
from domain.records.schema import WorkoutSetInput
from domain.records.service import WorkoutRecordsService
from domain.stats.schema import PersonalBest
from domain.stats.service import StatsService
from graph.context import RECENT_SESSION_LIMIT, MemoryAssembler, MemoryContext
from storage.db import Database

#: 注入的业务日期：趋势窗口与停训天数都不从系统时钟取「今天」。
BUSINESS_DAY = date(2026, 6, 30)
CREATED_AT = "2026-06-01T08:00:00+08:00"

SQUAT = "barbell-back-squat"  # 杠铃背蹲：外加重量（杠铃总重）
SQUAT_CONVENTION = "barbell_includes_bar_total"
PULL_UP = "pull-up"  # 纯自重引体
PLANK = "plank"  # 平板支撑：计时

#: 讨论总结 §5.2／§14 冻结的六类装配内容，字段顺序即读取清单顺序。
SIX_CATEGORIES = (
    "profile",
    "active_plan",
    "recent_sessions",
    "personal_bests",
    "trend_summary",
    "request",
)


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _insert_plan(
    db: Database, *, version: int, status: str, structured_content: str = "{}"
) -> int:
    """直接 SQL 写入一个计划版本行（正式计划写入口留 Stage 5），返回计划身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, structured_content, created_at,"
            " confirmed_at) VALUES (?, ?, ?, ?, ?)",
            (
                version,
                status,
                structured_content,
                CREATED_AT,
                CREATED_AT if status == "active" else None,
            ),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


def _squat(weight_kg: float) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=SQUAT,
        set_no=1,
        reps=5,
        set_type="work",
        load_convention=SQUAT_CONVENTION,
        weight_kg=weight_kg,
    )


def _pull_up(reps: int) -> WorkoutSetInput:
    return WorkoutSetInput(exercise_id=PULL_UP, set_no=1, reps=reps, set_type="work")


def _plank(duration_seconds: int) -> WorkoutSetInput:
    return WorkoutSetInput(
        exercise_id=PLANK,
        set_no=1,
        reps=None,
        set_type="work",
        duration_seconds=duration_seconds,
    )


async def _add_session(
    db: Database, performed_on: date, sets: tuple[WorkoutSetInput, ...]
) -> int:
    record = await WorkoutRecordsService(db).create(performed_on, sets)
    return record.id


async def _assemble(
    db: Database,
    request: str = "生成计划",
    *,
    business_day: date = BUSINESS_DAY,
    exercise_ids: tuple[str, ...] | None = None,
) -> MemoryContext:
    return await MemoryAssembler(db).assemble(
        request, business_day=business_day, exercise_ids=exercise_ids
    )


# ---------- 装配范围：恰好六类 ----------


def test_assembled_context_has_exactly_the_six_categories() -> None:
    """输出恰好六类内容：不加统计缓存、聊天摘要或第二个训练历史字段。"""
    assert tuple(field.name for field in fields(MemoryContext)) == SIX_CATEGORIES


async def test_empty_database_still_assembles_the_six_categories_without_faking_facts(
    tmp_path: Path,
) -> None:
    """空库也装配六类：画像／active 计划／近期训练为空是「确实缺失」，不用空值伪造，请求原样回显。"""
    db = await _migrated(tmp_path / "empty.db")
    try:
        context = await _assemble(db, "看看进步")

        assert isinstance(context, MemoryContext)
        assert context.request == "看看进步"
        assert context.profile is None
        assert context.active_plan is None
        assert context.recent_sessions == ()
        assert context.personal_bests == ()
        assert context.trend_summary.weight_change.status == "no_data"
        assert context.trend_summary.days_since_last_workout.status == "no_data"
    finally:
        await db.close()


# ---------- 最近四次训练：有界读取 ----------


async def test_recent_sessions_are_bounded_to_the_newest_four_with_their_own_sets(
    tmp_path: Path,
) -> None:
    """超过四次时只装配最新四次（同 ``performed_on`` 再按身份取大）及其自己的组，不退化成全量截断。"""
    db = await _migrated(tmp_path / "bounded.db")
    try:
        six: list[int] = []
        for day, weight in (
            (date(2026, 6, 1), 90.0),
            (date(2026, 6, 2), 95.0),
            (date(2026, 6, 3), 100.0),
            (date(2026, 6, 4), 105.0),
            (date(2026, 6, 5), 110.0),
        ):
            six.append(await _add_session(db, day, (_squat(weight),)))
        # 同一天第二次训练：日期相同，按训练身份取大，各算一次。
        same_day = await _add_session(db, date(2026, 6, 5), (_squat(115.0),))
        six.append(same_day)

        context = await _assemble(db)

        assert [session.id for session in context.recent_sessions] == [
            same_day,
            six[4],
            six[3],
            six[2],
        ]
        assert RECENT_SESSION_LIMIT == 4
        assert [session.performed_on for session in context.recent_sessions] == [
            date(2026, 6, 5),
            date(2026, 6, 5),
            date(2026, 6, 4),
            date(2026, 6, 3),
        ]
        # 每次训练保留自己的全部组；被排除的两次训练的组不进入装配结果。
        weights = [
            set_.weight_kg
            for session in context.recent_sessions
            for set_ in session.sets
        ]
        assert weights == [115.0, 110.0, 105.0, 100.0]
        assert len(await WorkoutRecordsService(db).list_all()) == 6
    finally:
        await db.close()


async def test_recent_sessions_return_only_existing_records(
    tmp_path: Path,
) -> None:
    """不足四次只返回实际存在的记录，并按 ``performed_on DESC, id DESC`` 最新在前。"""
    db = await _migrated(tmp_path / "few.db")
    try:
        older = await _add_session(db, date(2026, 6, 3), (_squat(90.0),))
        newer = await _add_session(db, date(2026, 6, 4), (_squat(92.5),))

        context = await _assemble(db)

        assert [session.id for session in context.recent_sessions] == [newer, older]
    finally:
        await db.close()


async def test_recent_sessions_reject_non_positive_limit(tmp_path: Path) -> None:
    """非正数 ``limit`` 直接失败：SQLite 的 ``LIMIT -1`` 是不限量，不能静默退化成全量读取。"""
    db = await _migrated(tmp_path / "limit.db")
    try:
        await _add_session(db, date(2026, 6, 3), (_squat(90.0),))

        with pytest.raises(ValueError):
            await WorkoutRecordsService(db).list_recent(0)
        with pytest.raises(ValueError):
            await WorkoutRecordsService(db).list_recent(-1)
    finally:
        await db.close()


# ---------- active 计划 ----------


async def test_only_the_active_plan_enters_and_opaque_content_is_not_interpreted(
    tmp_path: Path,
) -> None:
    """只有 active 计划进入上下文；``structured_content`` 按不透明 JSON 原样带出，本层不解释形状。"""
    db = await _migrated(tmp_path / "plans.db")
    try:
        await _insert_plan(db, version=1, status="draft")
        await _insert_plan(db, version=2, status="archived")
        opaque = '{"尚未定形状": [1, 2, 3]}'
        active_id = await _insert_plan(
            db, version=3, status="active", structured_content=opaque
        )

        context = await _assemble(db)

        assert context.active_plan == await PlanReadService(db).get_active()
        assert isinstance(context.active_plan, Plan)
        assert context.active_plan.id == active_id
        assert context.active_plan.status == "active"
        assert context.active_plan.structured_content == {"尚未定形状": [1, 2, 3]}
    finally:
        await db.close()


async def test_draft_plan_is_not_used_as_a_substitute_for_the_active_plan(
    tmp_path: Path,
) -> None:
    """只有 draft 计划时 active 就是缺失：不拿 draft 顶替，也不从草稿猜动作。"""
    db = await _migrated(tmp_path / "draft-only.db")
    try:
        await _insert_plan(db, version=1, status="draft")

        context = await _assemble(db)

        assert context.active_plan is None
    finally:
        await db.close()


# ---------- 画像 ----------


async def test_profile_is_read_through_the_profile_service(
    tmp_path: Path,
) -> None:
    """复用 ``ProfileService.read()``：未建档保持缺失语义，建档后装配到同一份三态事实。"""
    db = await _migrated(tmp_path / "profile.db")
    try:
        assert (await _assemble(db)).profile is None

        await ProfileService(db).update(
            Profile(
                training_goal=Fact.known("增肌"),
                weekly_frequency=Fact.known(4),
                forbidden_exercise_ids=Fact.denied(),
            )
        )

        context = await _assemble(db)

        assert context.profile == await ProfileService(db).read()
        assert context.profile is not None
        assert context.profile.training_goal == Fact.known("增肌")
        assert context.profile.weekly_frequency == Fact.known(4)
        assert context.profile.forbidden_exercise_ids == Fact.denied()
        assert context.profile.known_injuries == Fact.unknown()
    finally:
        await db.close()


# ---------- 相关动作 PB ----------

#: 三条 PB 各自来源训练的发生日期，与下面写入的事实一一对应。
SOURCED_ON = {
    SQUAT: date(2026, 6, 20),
    PULL_UP: date(2026, 6, 21),
    PLANK: date(2026, 6, 22),
}


async def test_personal_bests_cover_all_exercises_and_filter_by_the_caller_ids(
    tmp_path: Path,
) -> None:
    """生成新计划装配全部已有动作 PB；给出 ``exercise_ids`` 时只装这些动作，且来源原样保留。"""
    db = await _migrated(tmp_path / "bests.db")
    try:
        squat_session = await _add_session(db, date(2026, 6, 20), (_squat(100.0),))
        pull_up_session = await _add_session(db, date(2026, 6, 21), (_pull_up(12),))
        plank_session = await _add_session(db, date(2026, 6, 22), (_plank(90),))
        all_bests = await StatsService(db).list_personal_bests()

        everything = await _assemble(db)
        related = await _assemble(db, exercise_ids=(PULL_UP,))
        none_related = await _assemble(db, exercise_ids=())

        # 全量装配不重算 PB：与 Stats 服务现算结果逐条相等。
        assert everything.personal_bests == all_bests
        assert {pb.exercise_id for pb in all_bests} == {SQUAT, PULL_UP, PLANK}
        # 修正后的 PB 类型组成：外加重量动作只有重量 PB、纯自重动作只有次数 PB、计时动作只有时长 PB。
        assert {(pb.exercise_id, pb.pb_type) for pb in all_bests} == {
            (SQUAT, "weight_pb"),
            (PULL_UP, "reps_pb"),
            (PLANK, "duration_pb"),
        }
        # 过滤只筛动作身份：纯自重引体保留自己的 PB，与无关动作不混。
        assert related.personal_bests == tuple(
            pb for pb in all_bests if pb.exercise_id == PULL_UP
        )
        assert related.personal_bests
        assert none_related.personal_bests == ()
        # 来源与日期原样带出，本层不派生新结果。
        source_session = {
            SQUAT: squat_session,
            PULL_UP: pull_up_session,
            PLANK: plank_session,
        }
        for pb in everything.personal_bests:
            assert isinstance(pb, PersonalBest)
            assert pb.workout_session_id == source_session[pb.exercise_id]
            assert pb.set_no == 1
            assert pb.performed_on == SOURCED_ON[pb.exercise_id]
    finally:
        await db.close()


# ---------- trend_summary ----------


async def test_trend_summary_matches_the_stats_service_at_the_injected_business_day(
    tmp_path: Path,
) -> None:
    """直接复用 ``StatsService.trend_summary(business_day)``：注入的业务日期决定停训天数。"""
    db = await _migrated(tmp_path / "trend.db")
    try:
        metrics = BodyMetricsService(db)
        await metrics.create(date(2026, 6, 1), 80.0, 20.0)
        await metrics.create(date(2026, 6, 10), 79.0)
        await _add_session(db, date(2026, 6, 20), (_squat(100.0),))
        stats = StatsService(db)

        context = await _assemble(db, business_day=date(2026, 6, 30))
        later = await _assemble(db, business_day=date(2026, 7, 10))

        assert context.trend_summary == await stats.trend_summary(date(2026, 6, 30))
        assert context.trend_summary.weight_change.status == "ok"
        assert context.trend_summary.weight_change.current == 79.0
        assert context.trend_summary.weight_change.previous == 80.0
        assert context.trend_summary.weight_change.change == -1.0
        # 体脂只有一条记录：显式 insufficient_data，不补 0、不伪造变化值。
        assert context.trend_summary.body_fat_change.status == "insufficient_data"
        assert context.trend_summary.days_since_last_workout.days == 10
        assert later.trend_summary.days_since_last_workout.days == 20
    finally:
        await db.close()


# ---------- 每次装配重读业务事实 ----------


async def test_assembly_reflects_modification_of_an_existing_workout(
    tmp_path: Path,
) -> None:
    """修改既有训练（日期与重量）后重新装配：近期训练、PB 数值与来源、停训天数立即换成新事实。"""
    db = await _migrated(tmp_path / "modified.db")
    try:
        session = await _add_session(db, date(2026, 6, 20), (_squat(100.0),))
        before = await _assemble(db, business_day=date(2026, 6, 30))
        assert [set_.weight_kg for set_ in before.recent_sessions[0].sets] == [100.0]
        (before_pb,) = before.personal_bests
        assert (before_pb.value, before_pb.performed_on) == (100.0, date(2026, 6, 20))
        assert before.trend_summary.days_since_last_workout.days == 10

        moved_on = date(2026, 6, 25)
        await WorkoutRecordsService(db).update(session, moved_on, (_squat(130.0),))

        after = await _assemble(db, business_day=date(2026, 6, 30))

        assert [record.id for record in after.recent_sessions] == [session]
        assert after.recent_sessions[0].performed_on == moved_on
        assert [set_.weight_kg for set_ in after.recent_sessions[0].sets] == [130.0]
        (after_pb,) = after.personal_bests
        assert (after_pb.exercise_id, after_pb.pb_type, after_pb.value) == (
            SQUAT,
            "weight_pb",
            130.0,
        )
        assert (after_pb.workout_session_id, after_pb.set_no, after_pb.performed_on) == (
            session,
            1,
            moved_on,
        )
        assert after.trend_summary.days_since_last_workout.days == 5
    finally:
        await db.close()


async def test_assembly_rereads_facts_after_record_deletion_and_profile_update(
    tmp_path: Path,
) -> None:
    """修改／删除训练或更新画像后，下一次装配立即反映新事实（无业务事实缓存）。"""
    db = await _migrated(tmp_path / "reread.db")
    try:
        keep = await _add_session(db, date(2026, 6, 20), (_squat(100.0),))
        drop = await _add_session(db, date(2026, 6, 21), (_squat(120.0),))
        before = await _assemble(db)
        assert [session.id for session in before.recent_sessions] == [drop, keep]

        await WorkoutRecordsService(db).delete(drop)
        await ProfileService(db).update(Profile(training_goal=Fact.known("力量")))

        after = await _assemble(db)

        assert [session.id for session in after.recent_sessions] == [keep]
        assert after.personal_bests
        assert all(pb.workout_session_id == keep for pb in after.personal_bests)
        assert after.profile is not None
        assert after.profile.training_goal == Fact.known("力量")
    finally:
        await db.close()
