"""Stage 3 S3-05：计划草稿纠错与丢弃。

验收对照（stage3.md §5 S3-05）：

- 仅允许纠正计划载荷已拍可变字段（日期／训练日／动作候选／组次／次数区间／RIR，F2-03）；
  整体替换后全量重解码重验证，revision+1；正式计划／日程／档案／业务版本不变。
- 不能借纠错改身份／来源／基线／状态／凭据；草稿 ``source_plan_version_id``、``mode``、
  取消预览与拟议档案补丁原样保留。
- 旧 revision 拒绝；非法目录引用与越界日期拒绝；终态（Committed／Discarded）不可纠错；
  Discarded 不可确认且重复丢弃幂等。
- 纠错／丢弃与确认写入共用唯一锁与事务串行；「当前目录候选」「补丁应用后的拟议条件」
  都取纠错当刻事实，不沿用创建快照。

边界（stage3.md §5 S3-05）：不实现确认事务与日程原子切换（S3-06）；本文件只经内部应用层、
``tmp_path`` 下的临时文件库。测试里对 ``business_drafts`` 的状态写入（提交替身）、
``exercises.recommendable`` 的翻转与「已确认计划」种子都是 S3-06/目录策展之前的替身，
不是生产写入旁路。
"""

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from app.confirm import ConfirmService
from app.draft_repo import DraftRepo
from app.drafts import (
    DraftKindMismatch,
    DraftNotCorrectable,
    DraftNotDiscardable,
    DraftRevisionConflict,
    DraftService,
    UnknownDraft,
)
from app.plan_drafts import PlanDraftService
from domain.actions.repo import ExerciseRepo
from domain.plan.rules import InvalidPlanPayload
from domain.plan.schema import (
    DisplaySnapshot,
    IntRange,
    PlanPayload,
    RepsPrescription,
    VerifiedLoad,
    WorkoutCycleSlot,
)
from domain.profile.repo import ProfileRepo
from domain.profile.schema import ActionRestriction, Fact, ProfilePatch
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage3_plan_drafts import (
    REVIEW_ON,
    STARTS_ON,
    _commit_profile,
    _payload,
    _profile,
    _seed_confirmed_plan,
)

CONVERSATION_ID = "c1"
# 纠错允许写到的草稿列；其余列（身份、来源、基线、拟议条件、补丁、状态、凭据）都不可变。
REVISABLE_ROW_FIELDS = frozenset({"proposed_plan_json", "revision", "updated_at"})


async def _row(db: Database, draft_id: str) -> dict[str, object]:
    """``business_drafts`` 原始行（列级不可变断言用）。"""

    async def op(conn):
        async with conn.execute(
            "SELECT * FROM business_drafts WHERE id = ?", (draft_id,)
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    return await db.under_lock(op)


async def _setup_plan_draft(
    db: Database,
    *,
    draft_id: str = "d1",
    patch: ProfilePatch | None = None,
    payload: PlanPayload | None = None,
) -> tuple[PlanDraftService, PlanPayload]:
    """建一条 Pending 计划草稿（S3-04 内部创建入口），返回服务与落库的 payload。"""
    await RunRepo(db).create_conversation(CONVERSATION_ID)
    await _commit_profile(db, _profile())
    service = PlanDraftService(db)
    preparation = await service.prepare_generation_input()
    if payload is None:
        payload = await _payload(db, _profile())
    await service.create_plan_draft(
        draft_id=draft_id,
        preparation=preparation,
        conversation_id=CONVERSATION_ID,
        run_id=None,
        payload=payload,
        starts_on=STARTS_ON,
        review_on=REVIEW_ON,
        business_date=date(2026, 9, 20),
        patch=patch,
    )
    return service, payload


async def _commit_standin(db: Database, *, draft_id: str, revision: int = 1) -> None:
    """模拟计划确认写凭据（S3-06 替身）：只走既有草稿 CAS 写入，不建计划版本/日程。"""
    async with db.transaction() as conn:
        await DraftRepo(db).record_commit_in_transaction(
            conn,
            draft_id=draft_id,
            committed_revision=revision,
            committed_business_version=2,
        )


async def _formal_counts(db: Database) -> tuple[int, int]:
    """正式计划版本与日程行数（纠错/丢弃不得新增正式事实）。"""

    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM plan_versions") as cursor:
            plans = int((await cursor.fetchone())[0])
        async with conn.execute("SELECT COUNT(*) FROM scheduled_sessions") as cursor:
            sessions = int((await cursor.fetchone())[0])
        return plans, sessions

    return await db.under_lock(op)


def _with_first_item(payload: PlanPayload, item) -> PlanPayload:
    """把 push 训练日的首个动作替换为 ``item``（模拟动作候选纠错）。"""
    workout = payload.plan_workouts[0]
    corrected = replace(workout, exercises=(item,) + workout.exercises[1:])
    return replace(payload, plan_workouts=(corrected,) + payload.plan_workouts[1:])


def _with_verified_first_load(
    payload: PlanPayload, *, basis_record_revision_id: str | None
) -> PlanPayload:
    """给首个动作发一个 verified 负荷（模拟「有可信记录」时的拟议剂量）。"""
    item = payload.plan_workouts[0].exercises[0]
    return _with_first_item(
        payload,
        replace(
            item,
            load=VerifiedLoad(
                value=60.0,
                unit="kg",
                load_notation="barbell_includes_bar_total",
                basis_record_revision_id=basis_record_revision_id,
            ),
        ),
    )


# ---------- 纠错：整体替换已拍可变字段并 revision+1 ----------


async def test_revise_replaces_dates_and_mutable_payload_and_bumps_revision(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db)
        item = payload.plan_workouts[0].exercises[0]
        corrected_item = replace(
            item,
            exercise_id="dumbbell-bench-press",
            display_snapshot=DisplaySnapshot(
                name="哑铃平板卧推",
                equipment_variant="dumbbell",
                load_convention="dumbbell_per_hand",
            ),
            prescription=RepsPrescription(
                work_sets=4,
                reps_range=IntRange(min=8, max=10),
                target_rir=IntRange(min=1, max=2),
            ),
        )
        corrected_payload = _with_first_item(payload, corrected_item)
        corrected_start = STARTS_ON + timedelta(days=7)
        corrected_review = REVIEW_ON + timedelta(days=7)

        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=corrected_payload,
            starts_on=corrected_start,
            review_on=corrected_review,
        )

        assert view.draft.revision == 2
        assert view.draft.status == "pending"
        assert (view.proposal.starts_on, view.proposal.review_on) == (
            corrected_start,
            corrected_review,
        )
        assert view.proposal.payload.plan_workouts[0].exercises[0] == corrected_item
        # 日程按更正后的日期与循环重投影（anchor 相位不变）
        assert view.schedules[0].scheduled_on == corrected_start
        assert all(day.scheduled_on < corrected_review for day in view.schedules)
        diff = {entry.field: entry for entry in view.plan_diff}
        assert diff["starts_on"].changed and diff["starts_on"].after == corrected_start
        assert diff["workouts"].after == corrected_payload.plan_workouts
        # 纠错不写正式事实、不推进业务版本
        assert await _formal_counts(db) == (0, 0)
        assert (await ProfileRepo(db).read()).context_version == 1


async def test_revise_preserves_identity_source_baseline_patch_and_cancellations(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _commit_profile(db, _profile())
        service = PlanDraftService(db)
        replacement_payload = await _payload(db, _profile())
        # 已确认旧计划（替换基线）：一条未来未锁定、一条已存储锁定、一条已取消
        await _seed_confirmed_plan(
            db,
            conversation_id=CONVERSATION_ID,
            plan_id="plan-v1",
            payload=replacement_payload,
            sessions=(
                ("s-future", "2026-09-25", None, None),
                ("s-locked", "2026-09-10", "2026-09-10T08:00:00+00:00", None),
                ("s-cancelled", "2026-09-26", None, "2026-09-21T08:00:00+00:00"),
            ),
        )
        patch = ProfilePatch(facts={"body_weight_kg": Fact.known(71.0)})
        preparation = await service.prepare_generation_input()
        created = await service.create_plan_draft(
            draft_id="d1",
            preparation=preparation,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            payload=replacement_payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=date(2026, 9, 20),
            patch=patch,
        )
        assert created.proposal.source_plan_version_id == "plan-v1"
        assert [
            item.scheduled_session_id for item in created.proposal.cancellations
        ] == ["s-future"]
        before = await _row(db, "d1")

        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=replacement_payload,
            starts_on=STARTS_ON + timedelta(days=1),
            review_on=REVIEW_ON + timedelta(days=1),
        )

        # 列级不可变：身份、来源、基线、拟议条件、补丁、状态、凭据、创建时间全部原样
        after = await _row(db, "d1")
        assert {
            key: value
            for key, value in after.items()
            if key not in REVISABLE_ROW_FIELDS
        } == {
            key: value
            for key, value in before.items()
            if key not in REVISABLE_ROW_FIELDS
        }
        assert after["revision"] == 2
        # 替换来源、模式、取消预览与补丁都取自存储行，不随纠错改
        assert view.proposal.source_plan_version_id == "plan-v1"
        assert view.proposal.mode == "regular"
        assert view.proposal.cancellations == created.proposal.cancellations
        assert view.proposed_profile_patch == patch
        assert view.proposed_profile == created.proposed_profile
        # 旧版日程与正式计划未被纠错触碰
        assert await _formal_counts(db) == (1, 3)


async def test_correcting_a_stale_draft_does_not_rebase_the_business_baseline(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db)
        await _commit_profile(db, _profile(weekly_frequency=Fact.known(4)))
        assert (await ProfileRepo(db).read()).context_version == 2

        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )

        assert view.draft.revision == 2
        assert view.draft.base_business_version == 1  # 不重基到最新正式版本
        assert (await ProfileRepo(db).read()).context_version == 2


# ---------- 纠错：旧 revision、非法内容与目录/条件复查 ----------


async def test_stale_seen_revision_is_rejected_without_any_write(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db)
        await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )
        current = await _row(db, "d1")

        for stale in (1, 0, -3):
            with pytest.raises(DraftRevisionConflict, match="revision"):
                await service.revise_plan_draft(
                    draft_id="d1",
                    seen_revision=stale,
                    payload=payload,
                    starts_on=STARTS_ON,
                    review_on=REVIEW_ON,
                )
            assert await _row(db, "d1") == current

        # 用当前 revision 仍可纠错：拒绝不产生半状态
        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=2,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )
        assert view.draft.revision == 3


async def test_illegal_refs_and_date_bounds_are_rejected_atomically(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db)
        first = payload.plan_workouts[0].exercises[0]
        bad_slot_cycle = replace(
            payload.calendar_cycle,
            slots=(WorkoutCycleSlot(workout_key="no-such-workout"),)
            + payload.calendar_cycle.slots[1:],
        )
        invalid_cases: tuple[tuple[str, dict], ...] = (
            (
                "目录外动作身份",
                {
                    "payload": _with_first_item(
                        payload, replace(first, exercise_id="no-such-action")
                    )
                },
            ),
            (
                "循环槽引用不存在的 workout_key",
                {"payload": replace(payload, calendar_cycle=bad_slot_cycle)},
            ),
            (
                "RIR 区间倒置",
                {
                    "payload": _with_first_item(
                        payload,
                        replace(
                            first,
                            prescription=RepsPrescription(
                                work_sets=3,
                                reps_range=IntRange(min=6, max=8),
                                target_rir=IntRange(min=3, max=1),
                            ),
                        ),
                    )
                },
            ),
            ("生效范围倒置", {"starts_on": REVIEW_ON, "review_on": STARTS_ON}),
            (
                "生效范围为空档（范围内无训练日）",
                {
                    "starts_on": STARTS_ON + timedelta(days=1),
                    "review_on": STARTS_ON + timedelta(days=2),
                },
            ),
            (
                "日期带时刻",
                {"starts_on": datetime(2026, 9, 14, 8, 0), "review_on": REVIEW_ON},
            ),
            ("payload 类型不符", {"payload": object()}),
        )
        baseline = await _row(db, "d1")
        for reason, overrides in invalid_cases:
            kwargs: dict = {
                "draft_id": "d1",
                "seen_revision": 1,
                "payload": payload,
                "starts_on": STARTS_ON,
                "review_on": REVIEW_ON,
            }
            kwargs.update(overrides)
            with pytest.raises(InvalidPlanPayload):
                await service.revise_plan_draft(**kwargs)
            assert await _row(db, "d1") == baseline, reason

        # 拒绝后同一 revision 仍可正常纠错
        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )
        assert view.draft.revision == 2


async def test_correction_allowlist_rejects_identity_source_and_provenance_edits(
    tmp_path: Path,
) -> None:
    """S3-05 纠错只放行已拍可变字段：身份、模板来源与记录来源引用一律拒绝。"""
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db)
        push = payload.plan_workouts[0]
        first = push.exercises[0]
        base_cycle = payload.calendar_cycle

        def _renamed_cycle(old: str, new: str):
            slots = tuple(
                replace(slot, workout_key=new)
                if isinstance(slot, WorkoutCycleSlot) and slot.workout_key == old
                else slot
                for slot in base_cycle.slots
            )
            return replace(base_cycle, slots=slots)

        # 每个用例都先构造出**结构上合法**的替代 payload：拒绝只能来自白名单
        invalid_cases: tuple[tuple[str, PlanPayload], ...] = (
            (
                "workout_key 改名（连同循环槽引用）",
                replace(
                    payload,
                    plan_workouts=(replace(push, workout_key="push2"),)
                    + payload.plan_workouts[1:],
                    calendar_cycle=_renamed_cycle("push", "push2"),
                ),
            ),
            (
                "item_key 改名",
                _with_first_item(payload, replace(first, item_key="push-99")),
            ),
            (
                "增删动作条目",
                replace(
                    payload,
                    plan_workouts=(
                        replace(
                            push,
                            exercises=push.exercises[:-1],
                        ),
                    )
                    + payload.plan_workouts[1:],
                ),
            ),
            (
                "模板来源 template_key",
                replace(payload, template_key="custom"),
            ),
            (
                "训练日名称",
                replace(
                    payload,
                    plan_workouts=(replace(push, name="新名字"),)
                    + payload.plan_workouts[1:],
                ),
            ),
            (
                "预计时长",
                replace(
                    payload,
                    plan_workouts=(
                        replace(
                            push,
                            estimated_minutes=push.estimated_minutes + 10,
                        ),
                    )
                    + payload.plan_workouts[1:],
                ),
            ),
            (
                "把校准负荷写成已验证负荷",
                _with_verified_first_load(payload, basis_record_revision_id=None),
            ),
        )
        baseline = await _row(db, "d1")
        for reason, candidate in invalid_cases:
            with pytest.raises(InvalidPlanPayload):
                await service.revise_plan_draft(
                    draft_id="d1",
                    seen_revision=1,
                    payload=candidate,
                    starts_on=STARTS_ON,
                    review_on=REVIEW_ON,
                )
            assert await _row(db, "d1") == baseline, reason

        # 白名单内的改动仍可纠错：组次／次数区间／RIR 与重量值
        corrected = replace(
            first,
            prescription=RepsPrescription(
                work_sets=5,
                reps_range=IntRange(min=5, max=7),
                target_rir=IntRange(min=2, max=3),
            ),
        )
        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=_with_first_item(payload, corrected),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )
        assert view.draft.revision == 2
        assert view.proposal.payload.plan_workouts[0].exercises[0] == corrected


async def test_correction_cannot_rewrite_verified_load_provenance(
    tmp_path: Path,
) -> None:
    """已验证负荷的重量值可纠，但记录来源引用（basis_record_revision_id）不可改写。"""
    async with open_database(tmp_path / "app.db") as db:
        verified_payload = _with_verified_first_load(
            await _payload(db, _profile()), basis_record_revision_id="record-rev-1"
        )
        service, payload = await _setup_plan_draft(db, payload=verified_payload)
        item = payload.plan_workouts[0].exercises[0]
        assert isinstance(item.load, VerifiedLoad)

        baseline = await _row(db, "d1")
        with pytest.raises(InvalidPlanPayload, match="basis_record_revision_id"):
            await service.revise_plan_draft(
                draft_id="d1",
                seen_revision=1,
                payload=_with_verified_first_load(
                    payload, basis_record_revision_id="record-rev-2"
                ),
                starts_on=STARTS_ON,
                review_on=REVIEW_ON,
            )
        assert await _row(db, "d1") == baseline

        # 重量值可纠、来源引用保持不变
        reweighted = replace(item, load=replace(item.load, value=65.0))
        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=_with_first_item(payload, reweighted),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )
        corrected_item = view.proposal.payload.plan_workouts[0].exercises[0]
        assert corrected_item == reweighted
        assert isinstance(corrected_item.load, VerifiedLoad)
        assert corrected_item.load.basis_record_revision_id == "record-rev-1"


async def test_revise_revalidates_against_current_recommendable_catalog(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db)
        # 目录策展替身：把草稿引用的动作移出可推荐集合（系统迁移之外无生产写入口）
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE exercises SET recommendable = 0 WHERE id = ?",
                ("cable-pushdown",),
            )
        assert not (await ExerciseRepo(db).get_by_id("cable-pushdown")).recommendable  # type: ignore[union-attr]

        baseline = await _row(db, "d1")
        with pytest.raises(InvalidPlanPayload):
            await service.revise_plan_draft(
                draft_id="d1",
                seen_revision=1,
                payload=payload,
                starts_on=STARTS_ON,
                review_on=REVIEW_ON,
            )
        assert await _row(db, "d1") == baseline

        # 换掉不再可推荐的动作候选后可以纠错（同 item_key，纠错按当刻目录重验、不沿用创建快照）
        push = payload.plan_workouts[0]
        dropped = push.exercises[-1]  # cable-pushdown
        assert dropped.exercise_id == "cable-pushdown"
        swapped = replace(
            push,
            exercises=push.exercises[:-1]
            + (
                replace(
                    dropped,
                    exercise_id="cable-overhead-triceps-extension",
                    display_snapshot=DisplaySnapshot(
                        name="绳索过顶臂屈伸",
                        equipment_variant="cable",
                        load_convention="machine_pin_displayed_value",
                    ),
                ),
            ),
        )
        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=replace(
                payload, plan_workouts=(swapped,) + payload.plan_workouts[1:]
            ),
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )
        assert view.draft.revision == 2


async def test_revise_revalidates_against_patch_applied_conditions(
    tmp_path: Path,
) -> None:
    patch = ProfilePatch(
        add_restrictions=(
            ActionRestriction(scope="specific_action", target="seated-cable-row"),
        )
    )
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db, patch=patch)
        row = payload.plan_workouts[1].exercises[1]  # barbell-bent-over-row
        corrected = replace(
            row,
            exercise_id="seated-cable-row",
            display_snapshot=DisplaySnapshot(
                name="坐姿绳索划船",
                equipment_variant="cable",
                load_convention="machine_pin_displayed_value",
            ),
        )
        corrected_workout = replace(
            payload.plan_workouts[1],
            exercises=payload.plan_workouts[1].exercises[:1]
            + (corrected,)
            + payload.plan_workouts[1].exercises[2:],
        )
        corrected_payload = replace(
            payload,
            plan_workouts=(payload.plan_workouts[0], corrected_workout)
            + payload.plan_workouts[2:],
        )
        baseline = await _row(db, "d1")

        with pytest.raises(InvalidPlanPayload, match="命中限制"):
            await service.revise_plan_draft(
                draft_id="d1",
                seen_revision=1,
                payload=corrected_payload,
                starts_on=STARTS_ON,
                review_on=REVIEW_ON,
            )
        assert await _row(db, "d1") == baseline

        # 换成不受拟议限制约束的动作可以纠错：补丁原样保留
        allowed = replace(
            corrected,
            exercise_id="lat-pulldown",
            display_snapshot=DisplaySnapshot(
                name="高位下拉",
                equipment_variant="cable",
                load_convention="machine_pin_displayed_value",
            ),
        )
        allowed_payload = replace(
            corrected_payload,
            plan_workouts=(
                corrected_payload.plan_workouts[0],
                replace(
                    corrected_workout,
                    exercises=corrected_workout.exercises[:1]
                    + (allowed,)
                    + corrected_workout.exercises[2:],
                ),
            )
            + corrected_payload.plan_workouts[2:],
        )
        view = await service.revise_plan_draft(
            draft_id="d1",
            seen_revision=1,
            payload=allowed_payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )
        assert view.draft.revision == 2
        assert view.proposed_profile_patch == patch


# ---------- 终态：不可纠错、不可丢弃、不可确认 ----------


async def test_terminal_drafts_cannot_be_revised(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db, draft_id="d-committed")
        await _commit_standin(db, draft_id="d-committed")
        with pytest.raises(DraftNotCorrectable, match="committed"):
            await service.revise_plan_draft(
                draft_id="d-committed",
                seen_revision=1,
                payload=payload,
                starts_on=STARTS_ON,
                review_on=REVIEW_ON,
            )
        committed = await _row(db, "d-committed")
        assert (committed["status"], committed["committed_revision"]) == (
            "committed",
            1,
        )

        preparation = await service.prepare_generation_input()
        await service.create_plan_draft(
            draft_id="d-discarded",
            preparation=preparation,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            payload=payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
            business_date=date(2026, 9, 20),
        )
        await service.discard_plan_draft(draft_id="d-discarded")
        with pytest.raises(DraftNotCorrectable, match="discarded"):
            await service.revise_plan_draft(
                draft_id="d-discarded",
                seen_revision=1,
                payload=payload,
                starts_on=STARTS_ON,
                review_on=REVIEW_ON,
            )
        assert (await _row(db, "d-discarded"))["status"] == "discarded"


async def test_committed_draft_cannot_be_discarded(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_plan_draft(db)
        await _commit_standin(db, draft_id="d1")

        with pytest.raises(DraftNotDiscardable, match="不可被丢弃撤销"):
            await service.discard_plan_draft(draft_id="d1")

        row = await _row(db, "d1")
        assert (
            row["status"],
            row["committed_revision"],
            row["committed_business_version"],
        ) == ("committed", 1, 2)


async def test_discard_changes_only_status_and_repeat_is_idempotent(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_plan_draft(db)
        before = await _row(db, "d1")

        discarded = await service.discard_plan_draft(draft_id="d1")
        after = await _row(db, "d1")

        assert discarded.draft.status == "discarded"
        assert after["status"] == "discarded"
        # 只改状态：revision、拟议内容、基线、补丁、凭据、创建时间原样（更新时间另算）
        assert {k: v for k, v in after.items() if k != "updated_at"} == {
            k: ("discarded" if k == "status" else v)
            for k, v in before.items()
            if k != "updated_at"
        }
        assert (
            discarded.draft.committed_revision,
            discarded.draft.committed_business_version,
        ) == (None, None)
        assert await _formal_counts(db) == (0, 0)

        repeat = await service.discard_plan_draft(draft_id="d1")
        assert repeat == discarded
        assert await _row(db, "d1") == after  # 重复丢弃不写入、不刷新时间戳
        assert await service.get_plan_draft("d1") == discarded


async def test_discarded_plan_draft_is_unconfirmable_through_confirm_guards(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_plan_draft(db)
        confirm = ConfirmService(db)
        # Pending 计划草稿本就不由档案确认入口提交（kind 分派在任何写入之前）
        with pytest.raises(DraftKindMismatch):
            await confirm.confirm_profile_draft(draft_id="d1", seen_revision=1)
        await service.discard_plan_draft(draft_id="d1")

        with pytest.raises(DraftKindMismatch):
            await confirm.confirm_profile_draft(draft_id="d1", seen_revision=1)

        assert (await _row(db, "d1"))["status"] == "discarded"
        assert await _formal_counts(db) == (0, 0)
        assert (await ProfileRepo(db).read()).context_version == 1


# ---------- 身份、kind 与并发串行 ----------


async def test_unknown_identity_and_kind_mismatch_are_rejected(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _commit_profile(db, _profile())
        plan_service = PlanDraftService(db)

        with pytest.raises(UnknownDraft):
            await plan_service.revise_plan_draft(
                draft_id="missing",
                seen_revision=1,
                payload=cast(PlanPayload, object()),
                starts_on=STARTS_ON,
                review_on=REVIEW_ON,
            )
        with pytest.raises(UnknownDraft):
            await plan_service.discard_plan_draft(draft_id="missing")

        # 档案草稿不可经计划草稿入口纠错／丢弃（kind 分派在任何写入之前）
        profile_service = DraftService(db)
        baseline = await profile_service.prepare_generation_baseline()
        await profile_service.create_profile_draft(
            draft_id="p1",
            generation_baseline=baseline,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            proposed=_profile(),
        )
        with pytest.raises(DraftKindMismatch):
            await plan_service.revise_plan_draft(
                draft_id="p1",
                seen_revision=1,
                payload=cast(PlanPayload, object()),
                starts_on=STARTS_ON,
                review_on=REVIEW_ON,
            )
        with pytest.raises(DraftKindMismatch):
            await plan_service.discard_plan_draft(draft_id="p1")
        assert (await _row(db, "p1"))["kind"] == "profile_update"


async def test_concurrent_revises_with_same_seen_revision_allow_exactly_one(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db)
        shifted_start = STARTS_ON + timedelta(days=7)
        shifted_review = REVIEW_ON + timedelta(days=7)

        results = await asyncio.wait_for(
            asyncio.gather(
                service.revise_plan_draft(
                    draft_id="d1",
                    seen_revision=1,
                    payload=payload,
                    starts_on=shifted_start,
                    review_on=shifted_review,
                ),
                service.revise_plan_draft(
                    draft_id="d1",
                    seen_revision=1,
                    payload=payload,
                    starts_on=STARTS_ON,
                    review_on=REVIEW_ON,
                ),
                return_exceptions=True,
            ),
            timeout=10,
        )

        views = [result for result in results if not isinstance(result, BaseException)]
        conflicts = [
            result for result in results if isinstance(result, DraftRevisionConflict)
        ]
        assert len(views) == 1 and len(conflicts) == 1
        final = await service.get_plan_draft("d1")
        assert final is not None
        assert final.draft.revision == 2  # 只递增一次，无重复叠加
        assert final.proposal == views[0].proposal


async def test_revise_and_discard_serialize_with_commit_writer(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, payload = await _setup_plan_draft(db)

        results = await asyncio.wait_for(
            asyncio.gather(
                service.revise_plan_draft(
                    draft_id="d1",
                    seen_revision=1,
                    payload=payload,
                    starts_on=STARTS_ON + timedelta(days=7),
                    review_on=REVIEW_ON + timedelta(days=7),
                ),
                _commit_standin(db, draft_id="d1"),
                return_exceptions=True,
            ),
            timeout=10,
        )

        row = await _row(db, "d1")
        revise_result, commit_result = results
        if isinstance(commit_result, BaseException):
            # 纠错先生效：提交 CAS 按旧 revision 落空，草稿保持 Pending
            assert isinstance(commit_result, RuntimeError)
            assert not isinstance(revise_result, BaseException)
            assert (row["status"], row["revision"]) == ("pending", 2)
            assert row["committed_revision"] is None
        else:
            # 提交先生效：纠错被终态拒绝，凭据保持
            assert isinstance(revise_result, DraftNotCorrectable)
            assert (row["status"], row["revision"]) == ("committed", 1)
            assert row["committed_revision"] == 1


async def test_concurrent_discards_are_idempotent_with_a_single_write(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service, _ = await _setup_plan_draft(db)

        results = await asyncio.wait_for(
            asyncio.gather(
                service.discard_plan_draft(draft_id="d1"),
                service.discard_plan_draft(draft_id="d1"),
            ),
            timeout=10,
        )

        first, second = results
        assert first == second
        assert first.draft.status == "discarded"
        assert await service.get_plan_draft("d1") == first
        assert await _formal_counts(db) == (0, 0)
