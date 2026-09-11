"""Stage 3 S3-08：当次安排接受落盘（``arrangement`` 草稿与确认）。

验收对照（stage3.md §5 S3-08、§4.2；04 4.3 与 04 验收 4–5）：

- 接受即落盘：确认事务写一条 ``arrangement_revisions``（完整目标快照 + 真实 ``accepted_at``
  + 来源草稿），业务版本恰好 +1，草稿 Committed 与凭据同事务。
- 临时调整不改长期计划：``plan_versions``／``scheduled_sessions``／``user_profile`` 零写入；
  后来的长期计划替换不改写已接受的当次目标，该次仍显示原计划目标与当次目标。
- 重启与幂等：关闭重开后安排与接受时间仍在；重复确认返回原结果、不追加修订、不推进版本。
- 不倒填：``accepted_at`` 是接受当刻的真实时间（落在确认调用前后窗口内），没有调用方传入
  时间的入口；再次接受只追加自己的 ``accepted_at``，不改写历史修订的时间。
- 锁定与接受分离：已到期锁定（含存储锁定标记）的日程仍可接受减组等调整，已取消的日程拒结。
- 拦截与守卫零写入：过期基线、旧 revision、错误的 kind、已丢弃草稿、越界调整、未知日程。

边界：全部经内部应用层（``ArrangementDraftService``／``ConfirmService``）与 ``tmp_path``
临时文件库，不接 HTTP、不触碰真实用户数据目录。用例里对 ``scheduled_sessions`` 的原始
UPDATE 只用于造出「已存储锁定／已取消」这类阶段外存储状态替身（记录与取消的正式写入分别归
S3-09 起与 S3-06），不是生产写入旁路。
"""

import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.arrangement_drafts import ArrangementDraftService
from app.confirm import (
    ArrangementCommitResult,
    ConfirmService,
    DraftDiscarded,
    DraftRevisionConflict,
    DraftStale,
)
from app.draft_repo import DraftRepo
from app.drafts import DraftKindMismatch, DraftService, UnknownDraft
from domain.plan.repo import PlanRepo, PlanVersionRecord
from domain.plan.rules import (
    ArrangementAdjustment,
    InvalidArrangementTarget,
    arrangement_target_exercises,
    validate_arrangement_target,
)
from domain.plan.schema import (
    ArrangementTarget,
    IntRange,
    NeedsCalibration,
    RepsPrescription,
)
from domain.profile.schema import Fact
from storage.db import Database
from tests.support import open_database
from tests.test_stage3_plan_confirm import (
    _confirm_plan,
    _context_version,
    _create_plan_draft,
    _draft_row,
    _formal_profile,
    _formal_profile_json,
    _mark_stored_locked,
    _plan_rows,
    _session_rows,
)
from tests.test_stage3_plan_drafts import REVIEW_ON, STARTS_ON, _profile

CONVERSATION_ID = "c1"
BENCH_ITEM_KEY = "push-01"  # PPL push 日第 1 项：平板杠铃卧推（计划 4 组）
REASON = "用户报告睡眠不足，接受减少两组"


# ---------- 原始行读取与阶段外存储状态替身 ----------


async def _arrangement_rows(db: Database, scheduled_session_id: str) -> list[dict]:
    """某日程的全部安排修订行（字面量 SQL，不拼接表名）。"""

    async def op(conn):
        async with conn.execute(
            "SELECT id, scheduled_session_id, revision_no, target_snapshot_json,"
            " source_draft_id, accepted_at FROM arrangement_revisions"
            " WHERE scheduled_session_id = ? ORDER BY revision_no",
            (scheduled_session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": str(row["id"]),
                "scheduled_session_id": str(row["scheduled_session_id"]),
                "revision_no": int(row["revision_no"]),
                "target_snapshot_json": str(row["target_snapshot_json"]),
                "source_draft_id": str(row["source_draft_id"]),
                "accepted_at": str(row["accepted_at"]),
            }
            for row in rows
        ]

    return await db.under_lock(op)


async def _arrangement_count(db: Database) -> int:
    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM arrangement_revisions") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _mark_cancelled(db: Database, session_id: str) -> None:
    """把某条日程标记为已取消（S3-06 替换流程的阶段外替身，正式取消归确认事务）。"""
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE scheduled_sessions SET cancelled_at = ? WHERE id = ?",
            ("2026-09-20T00:00:00+00:00", session_id),
        )


# ---------- 准备：正式档案 + 已确认计划 ----------


async def _profile_and_plan(db: Database) -> PlanVersionRecord:
    """经真实链路建立正式档案并确认首个计划，返回当前正式计划版本。"""
    await _formal_profile(db)
    await _create_plan_draft(db, draft_id="plan-draft-1")
    await _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON)
    current = await PlanRepo(db).read_current()
    assert current is not None
    return current


async def _push_session(db: Database, plan_version_id: str):
    """该计划版本的第一条 push 应训练名额（安排必须绑定具体计划版本与训练日）。"""
    sessions = await PlanRepo(db).list_sessions(plan_version_id)
    for session in sessions:
        if session.plan_workout_key == "push":
            return session
    raise AssertionError("计划版本里没有 push 训练日")


async def _create_arrangement(
    db: Database,
    *,
    draft_id: str,
    session_id: str,
    work_sets: int | None = 2,
    target_rir: IntRange | None = None,
    item_key: str = BENCH_ITEM_KEY,
    reason: str | None = REASON,
):
    """按准备快照创建一条 Pending 安排草稿：默认把卧推从计划的 4 组减到 2 组。

    ``work_sets`` 为 None 时不减组（可单测 RIR 调整或“无调整”场景）。
    """
    service = ArrangementDraftService(db)
    preparation = await service.prepare_input()
    adjustments = (
        ()
        if work_sets is None and target_rir is None
        else (
            ArrangementAdjustment(
                item_key=item_key, work_sets=work_sets, target_rir=target_rir
            ),
        )
    )
    return await service.create_arrangement_draft(
        draft_id=draft_id,
        preparation=preparation,
        conversation_id=CONVERSATION_ID,
        run_id=None,
        scheduled_session_id=session_id,
        adjustments=adjustments,
        adjustment_reason=reason,
    )


async def _confirm_arrangement(
    db: Database, *, draft_id: str, seen_revision: int = 1
) -> ArrangementCommitResult:
    return await ConfirmService(db).confirm_arrangement_draft(
        draft_id=draft_id, seen_revision=seen_revision
    )


# ---------- 草稿创建：只拟议，不落正式事实 ----------


async def test_creating_an_arrangement_draft_binds_the_plan_and_writes_nothing(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        plans = await _plan_rows(db)
        sessions = await _session_rows(db)
        profile_before = await _formal_profile_json(db)

        view = await _create_arrangement(
            db, draft_id="arr-draft-1", session_id=session.id
        )

        assert view.draft.kind == "arrangement"
        assert view.draft.status == "pending"
        assert view.plan_version.id == plan.id
        assert view.session.id == session.id
        # 完整目标：绑定 + 计划目标（原计划）+ 当次目标
        assert view.target.plan_version_id == plan.id
        assert view.target.scheduled_session_id == session.id
        assert view.target.scheduled_on == session.scheduled_on
        assert view.target.adjustment_reason == REASON
        planned = {item.item_key: item for item in view.planned_workout.exercises}
        target = {item.item_key: item for item in view.target.exercises}
        assert planned[BENCH_ITEM_KEY].prescription.work_sets == 4
        assert target[BENCH_ITEM_KEY].prescription.work_sets == 2
        # 未调整条目原样照抄计划目标（完整快照，不是差异补丁）
        for item_key, planned_item in planned.items():
            if item_key != BENCH_ITEM_KEY:
                assert target[item_key] == planned_item
        # 只拟议：正式表与业务版本零变化
        assert await _arrangement_count(db) == 0
        assert await _context_version(db) == 2
        assert await _plan_rows(db) == plans
        assert await _session_rows(db) == sessions
        assert await _formal_profile_json(db) == profile_before

        # 查询按身份与会话两种入口都可读，且读回内容一致
        again = await ArrangementDraftService(db).get_arrangement_draft("arr-draft-1")
        assert again is not None
        assert again.target == view.target
        listed = await ArrangementDraftService(db).list_arrangement_drafts(
            CONVERSATION_ID
        )
        assert [item.draft.id for item in listed] == ["arr-draft-1"]


# ---------- 接受即落盘 ----------


async def test_accepting_writes_the_complete_snapshot_with_a_real_accept_time(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)
        plans = await _plan_rows(db)
        sessions = await _session_rows(db)
        profile_before = await _formal_profile_json(db)

        before = datetime.now(UTC)
        result = await _confirm_arrangement(db, draft_id="arr-draft-1")
        after = datetime.now(UTC)

        # 凭据：草稿已提交、业务版本恰好 +1（档案 1 + 计划 2 + 本次 3）
        assert result.draft_id == "arr-draft-1"
        assert result.committed_revision == 1
        assert result.committed_business_version == 3
        assert result.scheduled_session_id == session.id
        assert result.arrangement_revision_no == 1
        assert (await _draft_row(db, "arr-draft-1"))["status"] == "committed"
        assert await _context_version(db) == 3

        # 真实接受时间：落在确认调用窗口内，不是训练日推得、也不是调用方传入；
        # 每次接受自己的时间与计划确认时间分列保存（记录／更正确认时间归 S3-11）
        accepted = datetime.fromisoformat(result.accepted_at)
        assert before <= accepted <= after
        assert abs((accepted - before).total_seconds()) < 60
        assert accepted > datetime.fromisoformat(plan.confirmed_at)

        # 一条完整目标快照：绑定沿用草稿、来源草稿、完整条目与调整后的组次
        rows = await _arrangement_rows(db, session.id)
        assert len(rows) == 1
        assert rows[0]["source_draft_id"] == "arr-draft-1"
        assert rows[0]["accepted_at"] == result.accepted_at
        latest = await PlanRepo(db).read_latest_arrangement(session.id)
        assert latest is not None
        assert latest.id == result.arrangement_revision_id
        assert latest.target.plan_version_id == plan.id
        assert latest.target.scheduled_on == session.scheduled_on
        assert len(latest.target.exercises) == len(
            plan.payload.plan_workouts[0].exercises
        )
        bench = next(
            item for item in latest.target.exercises if item.item_key == BENCH_ITEM_KEY
        )
        assert bench.prescription.work_sets == 2

        # 临时调整不改长期计划：计划版本、日程、档案零写入
        assert await _plan_rows(db) == plans
        assert await _session_rows(db) == sessions
        assert await _formal_profile_json(db) == profile_before


async def test_restart_keeps_the_arrangement_and_duplicate_confirmation_is_idempotent(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)
        first = await _confirm_arrangement(db, draft_id="arr-draft-1")
        rows = await _arrangement_rows(db, session.id)
        plans = await _plan_rows(db)
        sessions = await _session_rows(db)

        # 重复确认：不追加修订、不再推进版本、返回原结果（旧 revision 也不改变幂等返回）
        again = await _confirm_arrangement(db, draft_id="arr-draft-1", seen_revision=99)
        assert again == first
        assert await _arrangement_rows(db, session.id) == rows
        assert await _context_version(db) == 3
        assert await _plan_rows(db) == plans
        assert await _session_rows(db) == sessions

    # 关闭重开：安排与接受时间仍在，重复确认仍返回同一份结果
    async with open_database(tmp_path / "app.db") as reopened:
        assert await _arrangement_rows(reopened, session.id) == rows
        latest = await PlanRepo(reopened).read_latest_arrangement(session.id)
        assert latest is not None and latest.accepted_at == first.accepted_at
        reopened_result = await _confirm_arrangement(reopened, draft_id="arr-draft-1")
        assert reopened_result == first
        assert await _arrangement_count(reopened) == 1
        assert await _context_version(reopened) == 3


async def test_later_plan_replacement_does_not_rewrite_the_accepted_target(
    tmp_path: Path,
) -> None:
    """04 验收 4：以后长期计划改动，该次仍显示原计划与当次安排。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)
        accepted = await _confirm_arrangement(db, draft_id="arr-draft-1")
        plan_before = (await _plan_rows(db))[plan.id]
        rows_before = await _arrangement_rows(db, session.id)

        # 替换：新计划版本启用（旧版全部日程已到期锁定，故只归档、不取消）
        await _create_plan_draft(db, draft_id="plan-draft-2")
        await _confirm_plan(db, draft_id="plan-draft-2", business_date=REVIEW_ON)

        replacement = await PlanRepo(db).read_current()
        assert replacement is not None and replacement.version == 2
        # 旧版本行只追加不改写；已接受安排仍指向旧版本，快照逐字节不变
        assert (await _plan_rows(db))[plan.id] == plan_before
        assert await _arrangement_rows(db, session.id) == rows_before
        latest = await PlanRepo(db).read_latest_arrangement(session.id)
        assert latest is not None and latest.id == accepted.arrangement_revision_id
        assert latest.target.plan_version_id == plan.id
        bench = next(
            item for item in latest.target.exercises if item.item_key == BENCH_ITEM_KEY
        )
        assert bench.prescription.work_sets == 2
        # 原计划目标（旧版本 payload）仍是 4 组：原计划与当次安排分列，互不改写
        old_bench = next(
            item
            for item in plan.payload.plan_workouts[0].exercises
            if item.item_key == BENCH_ITEM_KEY
        )
        assert old_bench.prescription.work_sets == 4


# ---------- 接受时间：真实、分列、不倒填 ----------


def test_confirmation_api_accepts_no_accept_time_from_the_caller() -> None:
    """不倒填的结构证据：确认入口只接受草稿身份与所见 revision，没有时间参数。"""
    parameters = inspect.signature(ConfirmService.confirm_arrangement_draft).parameters
    assert set(parameters) == {"self", "draft_id", "seen_revision"}


async def test_each_acceptance_keeps_its_own_time_and_never_backdates_history(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)
        first = await _confirm_arrangement(db, draft_id="arr-draft-1")
        first_rows = await _arrangement_rows(db, session.id)

        # 再次接受（进一步减到 1 组）：追加修订、不覆盖历史修订的接受时间
        await _create_arrangement(
            db, draft_id="arr-draft-2", session_id=session.id, work_sets=1
        )
        second = await _confirm_arrangement(db, draft_id="arr-draft-2")

        assert first.arrangement_revision_no == 1
        assert second.arrangement_revision_no == 2
        assert second.committed_business_version == 4  # 每次接受恰好 +1
        rows = await _arrangement_rows(db, session.id)
        assert len(rows) == 2
        assert rows[0] == first_rows[0]  # 历史修订行不被改写
        assert rows[0]["accepted_at"] == first.accepted_at
        first_time = datetime.fromisoformat(first.accepted_at)
        second_time = datetime.fromisoformat(second.accepted_at)
        assert second_time >= first_time
        # 两次接受各自保留自己的接受时间（记录／更正确认时间另表另列，S3-11）
        assert rows[1]["accepted_at"] == second.accepted_at
        latest = await PlanRepo(db).read_latest_arrangement(session.id)
        assert latest is not None and latest.revision_no == 2


# ---------- 锁定与取消 ----------


async def test_locked_session_still_accepts_a_temporary_adjustment(
    tmp_path: Path,
) -> None:
    """04 验收 8：日程锁定不等于当次处方锁定，执行前减组仍可接受并立即落盘。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _mark_stored_locked(db, plan_version_id=plan.id, on=session.scheduled_on)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)

        result = await _confirm_arrangement(db, draft_id="arr-draft-1")

        assert result.arrangement_revision_no == 1
        locked = next(
            row
            for row in (await _session_rows(db)).values()
            if row["plan_version_id"] == plan.id
            and row["scheduled_on"] == session.scheduled_on.isoformat()
        )
        assert locked["locked_at"] is not None  # 锁定标记保持，接受不改日程锁定状态


async def test_cancelled_session_is_refused_without_writes(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)
        await _mark_cancelled(db, session.id)

        with pytest.raises(InvalidArrangementTarget):
            await _confirm_arrangement(db, draft_id="arr-draft-1")

        assert await _arrangement_count(db) == 0
        assert await _context_version(db) == 2
        assert (await _draft_row(db, "arr-draft-1"))["status"] == "pending"
        # 已取消日程的历史草稿仍可查询（只是不能再接受）
        view = await ArrangementDraftService(db).get_arrangement_draft("arr-draft-1")
        assert view is not None and view.session.cancelled_at is not None
        # 已取消日程也不能新建安排草稿
        with pytest.raises(InvalidArrangementTarget):
            await _create_arrangement(db, draft_id="arr-draft-2", session_id=session.id)


# ---------- 拦截与守卫：零写入 ----------


async def test_stale_baseline_and_old_revision_are_refused(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)

        # 另一条真实档案确认推进业务版本：安排草稿的基线随即过期
        drafts = DraftService(db)
        baseline = await drafts.prepare_generation_baseline()
        await drafts.create_profile_draft(
            draft_id="profile-draft-2",
            generation_baseline=baseline,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            proposed=_profile(weekly_frequency=Fact.known(4)),
        )
        await ConfirmService(db).confirm_profile_draft(
            draft_id="profile-draft-2", seen_revision=1
        )
        with pytest.raises(DraftStale):
            await _confirm_arrangement(db, draft_id="arr-draft-1")
        assert await _arrangement_count(db) == 0
        assert (await _draft_row(db, "arr-draft-1"))["status"] == "pending"

        # 新基线上重新准备草稿：旧 revision 被拒（同一份内容不得静默覆盖）
        await _create_arrangement(db, draft_id="arr-draft-2", session_id=session.id)
        with pytest.raises(DraftRevisionConflict):
            await _confirm_arrangement(db, draft_id="arr-draft-2", seen_revision=99)
        assert await _arrangement_count(db) == 0
        assert await _draft_row(db, "arr-draft-2") == {
            "status": "pending",
            "revision": 1,
            "committed_revision": None,
            "committed_business_version": None,
        }


async def test_kind_discarded_and_unknown_guards(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)

        # 档案与计划确认入口不认安排草稿（kind 分派在任何写入之前）
        with pytest.raises(DraftKindMismatch):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="arr-draft-1", seen_revision=1
            )
        with pytest.raises(DraftKindMismatch):
            await ConfirmService(db).confirm_plan_draft(
                draft_id="arr-draft-1", seen_revision=1, business_date=STARTS_ON
            )
        with pytest.raises(UnknownDraft):
            await _confirm_arrangement(db, draft_id="d-missing")
        assert await _arrangement_count(db) == 0
        assert await _context_version(db) == 2

        # 已丢弃草稿不可确认（丢弃本身只改状态，S3-08 不提供安排草稿丢弃入口）
        async with db.transaction() as conn:
            await DraftRepo(db).record_discard_in_transaction(
                conn, draft_id="arr-draft-1"
            )
        with pytest.raises(DraftDiscarded):
            await _confirm_arrangement(db, draft_id="arr-draft-1")
        assert await _arrangement_count(db) == 0
        assert await _context_version(db) == 2


async def test_concurrent_confirmation_of_one_draft_writes_exactly_once(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)

        results = await asyncio.gather(
            _confirm_arrangement(db, draft_id="arr-draft-1"),
            _confirm_arrangement(db, draft_id="arr-draft-1"),
        )

        assert results[0] == results[1]
        assert await _arrangement_count(db) == 1
        assert await _context_version(db) == 3


# ---------- 目标校验：只允许已拍可变字段 ----------


async def test_target_changes_outside_the_decided_fields_are_refused(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)

        # 越界动作条目、缺原因、未知日程、无计划的准备输入一律拒绝且零写入
        with pytest.raises(InvalidArrangementTarget):
            await _create_arrangement(
                db,
                draft_id="arr-bad-item",
                session_id=session.id,
                item_key="push-99",
            )
        with pytest.raises(InvalidArrangementTarget):
            await _create_arrangement(
                db, draft_id="arr-no-reason", session_id=session.id, reason=None
            )
        with pytest.raises(InvalidArrangementTarget):
            await _create_arrangement(
                db, draft_id="arr-unknown-session", session_id="s-missing"
            )
        assert await _arrangement_count(db) == 0
        assert await _context_version(db) == 2
        assert [
            item.draft.id
            for item in (
                await ArrangementDraftService(db).list_arrangement_drafts(
                    CONVERSATION_ID
                )
            )
        ] == []


# ---------- 调整方向：只允许更安全方向（用户已拍 B） ----------


async def test_set_increase_is_refused_and_set_decrease_is_accepted(
    tmp_path: Path,
) -> None:
    """组次只减不增：减组是已拍当次调整，增组不是。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)

        with pytest.raises(InvalidArrangementTarget):
            await _create_arrangement(
                db,
                draft_id="arr-more-sets",
                session_id=session.id,
                work_sets=5,
            )
        assert await _arrangement_count(db) == 0
        assert await _context_version(db) == 2
        assert (
            await ArrangementDraftService(db).list_arrangement_drafts(CONVERSATION_ID)
            == ()
        )

        accepted = await _create_arrangement(
            db, draft_id="arr-fewer-sets", session_id=session.id, work_sets=2
        )
        planned_bench = next(
            item
            for item in accepted.planned_workout.exercises
            if item.item_key == BENCH_ITEM_KEY
        )
        target_bench = next(
            item
            for item in accepted.target.exercises
            if item.item_key == BENCH_ITEM_KEY
        )
        assert planned_bench.prescription.work_sets == 4
        assert target_bench.prescription.work_sets == 2
        result = await _confirm_arrangement(db, draft_id="arr-fewer-sets")
        assert result.arrangement_revision_no == 1
        latest = await PlanRepo(db).read_latest_arrangement(session.id)
        assert latest is not None
        assert (
            next(
                item
                for item in latest.target.exercises
                if item.item_key == BENCH_ITEM_KEY
            ).prescription.work_sets
            == 2
        )


async def test_rir_decrease_is_refused_and_rir_increase_is_accepted(
    tmp_path: Path,
) -> None:
    """目标 RIR 只增不减（计划卧推 1-3）：提高 RIR 更保守、合法；降低拒绝。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)

        for draft_id, rir in (
            ("arr-lower-rir-min", IntRange(0, 3)),
            ("arr-lower-rir-max", IntRange(1, 2)),
        ):
            with pytest.raises(InvalidArrangementTarget):
                await _create_arrangement(
                    db,
                    draft_id=draft_id,
                    session_id=session.id,
                    work_sets=None,
                    target_rir=rir,
                )
        assert await _arrangement_count(db) == 0
        assert await _context_version(db) == 2

        accepted = await _create_arrangement(
            db,
            draft_id="arr-higher-rir",
            session_id=session.id,
            work_sets=None,
            target_rir=IntRange(2, 4),
        )
        target_bench = next(
            item
            for item in accepted.target.exercises
            if item.item_key == BENCH_ITEM_KEY
        )
        assert target_bench.prescription.work_sets == 4  # 未给组次调整：保持计划值
        target_prescription = target_bench.prescription
        assert isinstance(target_prescription, RepsPrescription)
        assert target_prescription.target_rir == IntRange(2, 4)
        result = await _confirm_arrangement(db, draft_id="arr-higher-rir")
        assert result.arrangement_revision_no == 1
        latest = await PlanRepo(db).read_latest_arrangement(session.id)
        assert latest is not None
        stored_bench = next(
            item for item in latest.target.exercises if item.item_key == BENCH_ITEM_KEY
        )
        stored_prescription = stored_bench.prescription
        assert isinstance(stored_prescription, RepsPrescription)
        assert stored_prescription.target_rir == IntRange(2, 4)


async def test_blank_adjustment_reason_raises_the_arrangement_error(
    tmp_path: Path,
) -> None:
    """空串与空白理由与 None 同口径：错误码必须是安排错误，不冒用计划 payload 错误。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)

        for draft_id, reason in (
            ("arr-empty-reason", ""),
            ("arr-blank-reason", "   "),
        ):
            with pytest.raises(InvalidArrangementTarget) as failure:
                await _create_arrangement(
                    db, draft_id=draft_id, session_id=session.id, reason=reason
                )
            # 精确类：不是 InvalidPlanPayload 的子类关系问题，而是根本不能用计划错误码
            assert type(failure.value) is InvalidArrangementTarget
        assert await _arrangement_count(db) == 0
        assert await _context_version(db) == 2
        assert (
            await ArrangementDraftService(db).list_arrangement_drafts(CONVERSATION_ID)
            == ()
        )


def test_domain_validation_enforces_safer_direction_only() -> None:
    """领域层同一入口：减组／提 RIR／持平合法，增组／降 RIR 拒绝；无 RIR 基线不得新造。"""
    planned = plan_push_workout_for_domain_test()  # 4 组，RIR 1-3
    binding = {
        "scheduled_session_id": "s-1",
        "plan_version_id": "v-1",
        "plan_workout_key": "push",
        "scheduled_on": STARTS_ON,
    }

    def target(workout, *, work_sets=None, rir=None) -> ArrangementTarget:
        return ArrangementTarget(
            exercises=arrangement_target_exercises(
                workout,
                (
                    ArrangementAdjustment(
                        item_key="push-01", work_sets=work_sets, target_rir=rir
                    ),
                ),
            ),
            adjustment_reason=REASON,
            **binding,
        )

    # 更安全方向与持平：合法
    validate_arrangement_target(target(planned, work_sets=2), workout=planned)
    validate_arrangement_target(target(planned, rir=IntRange(2, 4)), workout=planned)
    validate_arrangement_target(
        target(planned, work_sets=4, rir=IntRange(1, 3)), workout=planned
    )
    # 反向：增组、降 RIR（任一端下降）均拒绝
    for bad in (
        target(planned, work_sets=5),
        target(planned, rir=IntRange(0, 3)),
        target(planned, rir=IntRange(1, 2)),
    ):
        with pytest.raises(InvalidArrangementTarget):
            validate_arrangement_target(bad, workout=planned)

    # 计划没有 RIR：保留基线合法，凭调整新造 RIR 拒绝；减组仍合法
    no_rir = plan_push_workout_for_domain_test(with_rir=False)
    unchanged = ArrangementTarget(
        exercises=tuple(no_rir.exercises), adjustment_reason=None, **binding
    )
    validate_arrangement_target(unchanged, workout=no_rir)
    validate_arrangement_target(target(no_rir, work_sets=2), workout=no_rir)
    with pytest.raises(InvalidArrangementTarget):
        validate_arrangement_target(target(no_rir, rir=IntRange(1, 3)), workout=no_rir)


def test_domain_validation_rejects_identity_and_range_rewrites() -> None:
    """计划目标之外只允许组次与目标 RIR：动作身份、次数区间、原因都不可绕过。"""
    planned = plan_push_workout_for_domain_test()
    target_items = arrangement_target_exercises(
        planned, (ArrangementAdjustment(item_key="push-01", work_sets=2),)
    )
    binding = {
        "scheduled_session_id": "s-1",
        "plan_version_id": "v-1",
        "plan_workout_key": "push",
        "scheduled_on": STARTS_ON,
    }

    def target(items, reason) -> ArrangementTarget:
        return ArrangementTarget(exercises=items, adjustment_reason=reason, **binding)

    # 合法：只减组且有原因
    validate_arrangement_target(target(target_items, REASON), workout=planned)
    # 合法：无调整（目标与计划逐条全等）不要求原因
    validate_arrangement_target(target(tuple(planned.exercises), None), workout=planned)

    bench = target_items[0]
    # 改动作身份、改次数区间、缺原因、把组次改成非法值，全部拒绝
    for bad_items, reason in (
        (broken(bench, exercise_id="pull-up"), REASON),
        (broken(bench, reps_min=1), REASON),
        (target_items, None),
        (broken(bench, work_sets=0), REASON),
    ):
        with pytest.raises(InvalidArrangementTarget):
            validate_arrangement_target(target(bad_items, reason), workout=planned)


def plan_push_workout_for_domain_test(*, with_rir: bool = True):
    """一个最小合法 push 训练日（借领域构造口径，不读库）；``with_rir=False`` 不带目标 RIR。"""
    from domain.plan.schema import (
        DisplaySnapshot,
        PlanExerciseItem,
        PlanWorkout,
        Progression,
    )

    item = PlanExerciseItem(
        item_key="push-01",
        exercise_id="barbell-bench-press",
        display_snapshot=DisplaySnapshot(
            name="平板杠铃卧推",
            equipment_variant="barbell",
            load_convention="barbell_includes_bar_total",
        ),
        record_type="external_load_reps",
        prescription=RepsPrescription(
            work_sets=4,
            reps_range=IntRange(6, 8),
            target_rir=IntRange(1, 3) if with_rir else None,
        ),
        load=NeedsCalibration(
            ("从最轻档位热身",),
            "能稳定完成次数下限",
            "疼痛或失稳时停止",
        ),
        progression=Progression(method="double_progression", rule="先加次数"),
    )
    return PlanWorkout(
        workout_key="push",
        name="推日",
        estimated_minutes=20,
        exercises=(item,),
    )


def broken(
    item,
    *,
    exercise_id: str | None = None,
    reps_min: int | None = None,
    work_sets: int | None = None,
):
    """按需改坏一个目标条目的一个字段（其余保持计划值），用于校验拒绝。"""
    from dataclasses import replace

    if exercise_id is not None:
        return (replace(item, exercise_id=exercise_id),)
    prescription = item.prescription
    if reps_min is not None:
        prescription = replace(
            prescription,
            reps_range=IntRange(reps_min, prescription.reps_range.max),
        )
    if work_sets is not None:
        prescription = replace(prescription, work_sets=work_sets)
    return (replace(item, prescription=prescription),)
