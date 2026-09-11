"""Stage 3 S3-07：到期锁定只读投影、历史计划查看与整份计划安全复核。

验收对照（stage3.md §5 S3-07、§4.3；04 验收 3、7、8）：

- 锁定按**固定业务时区**的业务日期规则强制判定：``business_date >= scheduled_on`` 即已锁定，
  存储锁定标记不是唯一依据；投影同时给出存储状态与生效状态。
- 停机跨过训练日、重启后即使未写锁定标记，该日程仍为已锁定，替换确认也不取消它（04 #3）。
- 当前计划与历史版本都能读取（旧计划仍可查看，不重激活）；已取消日程保留在投影里。
- 请求「基于计划的指导」前复核**整份**计划：最新限制冲突阻断、红旗独立阻断；限制未收集只给
  需澄清项，不当作「无冲突」；计划内动作读不到目录身份时 fail-closed。

固定时钟：业务日期一律由 ``storage.setting_repo.business_date`` 按库内固定业务时区从注入的
绝对时刻算出（复用 S0 时区设施），不取真实「今天」。

边界：只经内部应用层与 ``tmp_path`` 临时文件库，不接 HTTP、不触碰真实用户数据目录。对
``exercises`` 的原始删除只用于造出「计划引用动作已不在目录」这一阶段外存储状态替身
（目录策展写入不在 Stage 3 范围），不是生产写入旁路。
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.confirm import ConfirmService
from app.drafts import DraftService
from app.plan_reads import PlanReadService
from domain.plan.repo import PlanRepo
from domain.profile.safety import RED_FLAG_BLOCK_ADVICE
from domain.profile.schema import ActionRestriction, Fact, Profile
from storage.db import Database
from storage.setting_repo import SettingRepo, business_date
from tests.support import open_database
from tests.test_stage3_plan_confirm import (
    OLD_SCHEDULED_ON,
    REPLACE_BUSINESS_DATE,
    STORED_LOCKED_ON,
    _confirm_plan,
    _context_version,
    _create_plan_draft,
    _formal_profile,
    _mark_stored_locked,
)
from tests.test_stage3_plan_drafts import REVIEW_ON, STARTS_ON, _profile

CONVERSATION_ID = "c1"
TZ_SHANGHAI = "Asia/Shanghai"
# 固定时钟：上海 2026-09-13 23:30 与 2026-09-14 00:30（同一 UTC 日的跨午夜两侧）。
BEFORE_MIDNIGHT = datetime(2026, 9, 13, 15, 30, tzinfo=UTC)
AFTER_MIDNIGHT = datetime(2026, 9, 13, 16, 30, tzinfo=UTC)
NEXT_TRAINING_DAY = date(2026, 9, 16)


async def _update_formal_profile(
    db: Database, *, draft_id: str, profile: Profile
) -> None:
    """经真实档案确认链路更新正式条件（一次确认推进 ``context_version`` 恰好 +1）。"""
    drafts = DraftService(db)
    baseline = await drafts.prepare_generation_baseline()
    await drafts.create_profile_draft(
        draft_id=draft_id,
        generation_baseline=baseline,
        conversation_id=CONVERSATION_ID,
        run_id=None,
        proposed=profile,
    )
    await ConfirmService(db).confirm_profile_draft(draft_id=draft_id, seen_revision=1)


async def _confirmed_plan(
    db: Database, *, business_date_value: date = STARTS_ON
) -> str:
    """建立正式档案并确认首个计划版本，返回其计划版本 id（真实确认编排，不走替身）。"""
    await _formal_profile(db)
    await _create_plan_draft(db, draft_id="plan-draft-1")
    result = await _confirm_plan(
        db, draft_id="plan-draft-1", business_date=business_date_value
    )
    return result.plan_version_id


def _session(view, scheduled_on: date):
    return next(
        item for item in view.sessions if item.session.scheduled_on == scheduled_on
    )


def _plan_exercise_ids(view) -> set[str]:
    return {
        item.exercise_id
        for workout in view.version.payload.plan_workouts
        for item in workout.exercises
    }


# ---------- 到期锁定：存储状态与生效状态 ----------


async def test_effective_lock_follows_the_business_date_rule(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan_version_id = await _confirmed_plan(db)
        service = PlanReadService(db)

        day_before = await service.read_current_plan(
            business_date=STARTS_ON - timedelta(days=1)
        )
        assert day_before is not None
        assert all(not item.lock.effective for item in day_before.sessions)
        assert _session(day_before, STARTS_ON).lock.by_business_date is False

        on_the_day = await service.read_current_plan(business_date=STARTS_ON)
        assert {
            item.session.scheduled_on
            for item in on_the_day.sessions
            if item.lock.effective
        } == {STARTS_ON}
        assert _session(on_the_day, STARTS_ON).lock.stored is False
        assert _session(on_the_day, STARTS_ON).lock.by_business_date is True

        # 判定来自日期规则而非存储标记：库内 locked_at 始终为 NULL（无后台锁定任务）
        stored = await PlanRepo(db).list_sessions(plan_version_id)
        assert all(item.locked_at is None for item in stored)


async def test_stored_lock_marker_is_exposed_alongside_the_date_rule(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan_version_id = await _confirmed_plan(db)
        stored_locked_id = await _mark_stored_locked(
            db, plan_version_id=plan_version_id, on=STORED_LOCKED_ON
        )

        view = await PlanReadService(db).read_current_plan(business_date=STARTS_ON)
        assert view is not None
        marked = next(
            item for item in view.sessions if item.session.id == stored_locked_id
        )
        assert marked.lock.stored is True
        assert marked.lock.by_business_date is False  # 尚未到期
        assert marked.lock.effective is True  # 存储标记（完成／漏练）同样视为已锁定
        # 同一次读取里，已到期（日期规则）与已存储锁定的两条各自独立可见
        assert {
            item.session.scheduled_on for item in view.sessions if item.lock.effective
        } == {STARTS_ON, STORED_LOCKED_ON}


async def test_restart_across_the_day_boundary_locks_and_protects_sessions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        settings = SettingRepo(db)
        await settings.initialize_business_timezone(lambda: TZ_SHANGHAI)
        timezone_name = await settings.get_business_timezone()
        assert timezone_name == TZ_SHANGHAI
        plan_version_id = await _confirmed_plan(
            db, business_date_value=business_date(BEFORE_MIDNIGHT, TZ_SHANGHAI)
        )

        before = await PlanReadService(db).read_current_plan(
            business_date=business_date(BEFORE_MIDNIGHT, TZ_SHANGHAI)
        )
        assert before is not None
        assert all(not item.lock.effective for item in before.sessions)

    # 停机跨过训练日：重开同一文件库，期间没有任何锁定任务写过标记
    async with open_database(path) as db:
        timezone_name = await SettingRepo(db).get_business_timezone()
        assert timezone_name == TZ_SHANGHAI  # 固定业务时区跨重启不变
        on_the_day = business_date(AFTER_MIDNIGHT, timezone_name)
        assert on_the_day == STARTS_ON  # 上海已跨午夜到训练日当天

        view = await PlanReadService(db).read_current_plan(business_date=on_the_day)
        assert view is not None
        assert [
            item.session.scheduled_on for item in view.sessions if item.lock.effective
        ] == [STARTS_ON]
        assert all(item.lock.stored is False for item in view.sessions)

        # 改期／删除仍被拒：替换确认按当刻业务日期重算取消集，只取消未来未锁定日程
        await _create_plan_draft(db, draft_id="plan-draft-2")
        await _confirm_plan(db, draft_id="plan-draft-2", business_date=on_the_day)
        old_sessions = await PlanRepo(db).list_sessions(plan_version_id)
        cancelled = {
            item.scheduled_on for item in old_sessions if item.cancelled_at is not None
        }
        assert STARTS_ON not in cancelled
        assert NEXT_TRAINING_DAY in cancelled

        archived = await PlanReadService(db).read_plan_version(
            plan_version_id, business_date=on_the_day
        )
        assert archived is not None
        protected = _session(archived, STARTS_ON)
        assert archived.is_current is False
        assert protected.cancelled is False
        assert protected.lock.effective is True


# ---------- 当前与历史版本读取 ----------


async def test_current_and_historical_reads_show_cancelled_and_current_plan(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        first_version_id = await _confirmed_plan(db)
        await _mark_stored_locked(
            db, plan_version_id=first_version_id, on=STORED_LOCKED_ON
        )
        await _create_plan_draft(db, draft_id="plan-draft-2")
        second = await _confirm_plan(
            db, draft_id="plan-draft-2", business_date=REPLACE_BUSINESS_DATE
        )
        service = PlanReadService(db)

        current = await service.read_current_plan(business_date=REPLACE_BUSINESS_DATE)
        assert current is not None
        assert current.is_current is True
        assert current.version.id == second.plan_version_id
        assert current.version.version == 2
        assert all(not item.cancelled for item in current.sessions)
        assert current.version.review_on == REVIEW_ON

        archived = await service.read_plan_version(
            first_version_id, business_date=REPLACE_BUSINESS_DATE
        )
        assert archived is not None
        assert archived.is_current is False
        assert {item.session.scheduled_on for item in archived.sessions} == set(
            OLD_SCHEDULED_ON
        )
        # 已取消的未来日程仍可查看（行不物理删除）；到期锁定与已存储锁定的名额未被取消
        assert {
            item.session.scheduled_on for item in archived.sessions if item.cancelled
        } == {
            on
            for on in OLD_SCHEDULED_ON
            if on > REPLACE_BUSINESS_DATE and on != STORED_LOCKED_ON
        }
        stored_locked = _session(archived, STORED_LOCKED_ON)
        assert stored_locked.cancelled is False
        assert stored_locked.lock.stored is True

        # 同一版本经当前入口读取时 is_current 为真；不存在的身份返回 None
        reread = await service.read_plan_version(
            second.plan_version_id, business_date=REPLACE_BUSINESS_DATE
        )
        assert reread is not None and reread.is_current is True
        assert (
            await service.read_plan_version(
                "no-such-version", business_date=REPLACE_BUSINESS_DATE
            )
            is None
        )


async def test_reads_return_none_without_a_formal_plan(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        service = PlanReadService(db)
        assert await service.read_current_plan(business_date=STARTS_ON) is None
        assert await service.read_current_plan_guidance(business_date=STARTS_ON) is None
        assert (
            await service.read_plan_version("no-such-version", business_date=STARTS_ON)
            is None
        )


# ---------- 「基于计划的指导」前置整份计划复核 ----------


async def test_guidance_blocks_the_whole_plan_on_the_latest_restriction_conflict(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _confirmed_plan(db)
        service = PlanReadService(db)

        unrestricted = await service.read_current_plan_guidance(business_date=STARTS_ON)
        assert unrestricted is not None
        assert unrestricted.safety.is_blocked is False
        assert unrestricted.safety.blocking_reasons == ()
        plan_exercise_ids = _plan_exercise_ids(unrestricted.plan)

        await _update_formal_profile(
            db,
            draft_id="profile-draft-2",
            profile=_profile(
                action_restrictions=Fact.known(
                    (ActionRestriction(scope="movement_pattern", target="水平推"),)
                )
            ),
        )
        version_before = await _context_version(db)

        guidance = await service.read_current_plan_guidance(business_date=STARTS_ON)
        assert guidance is not None

        conflicts = {hit.exercise_id for hit in guidance.safety.restriction_conflicts}
        assert conflicts
        assert conflicts <= plan_exercise_ids
        assert "barbell-bench-press" in conflicts  # 计划内动作，不只是当天训练日
        assert guidance.safety.red_flags.is_blocked is False  # 无红旗仍阻断
        assert guidance.safety.is_blocked is True
        assert len(guidance.safety.blocking_reasons) == len(
            guidance.safety.restriction_conflicts
        )
        assert all(
            "命中最新限制" in reason for reason in guidance.safety.blocking_reasons
        )
        assert guidance.plan.is_current is True  # 旧计划仍可查看
        assert await _context_version(db) == version_before  # 复核只读，不推进版本


async def test_red_flag_blocks_independently_of_the_restriction_check(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _confirmed_plan(db)
        await _update_formal_profile(
            db,
            draft_id="profile-draft-2",
            profile=_profile(body_conditions=Fact.known(("胸部异常不适",))),
        )

        guidance = await PlanReadService(db).read_current_plan_guidance(
            business_date=STARTS_ON
        )
        assert guidance is not None

        assert guidance.safety.red_flags.is_blocked is True
        assert guidance.safety.restriction_conflicts == ()  # 限制维度通过仍阻断
        assert guidance.safety.is_blocked is True
        assert len(guidance.safety.blocking_reasons) == 1
        assert RED_FLAG_BLOCK_ADVICE in guidance.safety.blocking_reasons[0]
        assert "胸部异常不适" in guidance.safety.blocking_reasons[0]
        assert guidance.safety.clarification_reasons == ()


async def test_restriction_and_red_flag_reasons_are_both_reported(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _confirmed_plan(db)
        await _update_formal_profile(
            db,
            draft_id="profile-draft-2",
            profile=_profile(
                action_restrictions=Fact.known(
                    (ActionRestriction(scope="movement_pattern", target="水平推"),)
                ),
                body_conditions=Fact.known(("锐痛",)),
            ),
        )

        guidance = await PlanReadService(db).read_current_plan_guidance(
            business_date=STARTS_ON
        )
        assert guidance is not None

        reasons = guidance.safety.blocking_reasons
        assert len(reasons) == len(guidance.safety.restriction_conflicts) + 1
        assert any("命中最新限制" in reason for reason in reasons)
        assert any(RED_FLAG_BLOCK_ADVICE in reason for reason in reasons)


async def test_uncollected_restrictions_are_clarification_not_a_conflict(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _confirmed_plan(db)
        await _update_formal_profile(
            db,
            draft_id="profile-draft-2",
            profile=_profile(action_restrictions=Fact.unknown()),
        )

        guidance = await PlanReadService(db).read_current_plan_guidance(
            business_date=STARTS_ON
        )
        assert guidance is not None

        assert guidance.safety.restrictions_unknown is True
        assert guidance.safety.is_blocked is False
        assert guidance.safety.blocking_reasons == ()
        assert any(
            "未收集" in reason for reason in guidance.safety.clarification_reasons
        )


async def test_guidance_fails_closed_when_a_planned_action_left_the_catalog(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _confirmed_plan(db)
        # 阶段外存储状态替身：目录策展写入（含删除）不在 Stage 3 范围
        async with db.transaction() as conn:
            await conn.execute(
                "DELETE FROM exercises WHERE id = ?", ("barbell-bench-press",)
            )

        guidance = await PlanReadService(db).read_current_plan_guidance(
            business_date=STARTS_ON
        )
        assert guidance is not None

        assert guidance.safety.unknown_exercise_ids == ("barbell-bench-press",)
        assert guidance.safety.is_blocked is True
        assert any(
            "目录内已读不到" in reason for reason in guidance.safety.blocking_reasons
        )
        assert guidance.safety.red_flags.is_blocked is False
