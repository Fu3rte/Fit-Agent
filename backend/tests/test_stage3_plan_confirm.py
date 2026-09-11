"""Stage 3 S3-06：计划草稿确认事务与日程原子切换。

验收对照（stage3.md §5 S3-06、§4.2；04 4.1–4.3）：

- 启用 v1：首个计划确认建立 ``plan_versions`` v1 与 ``[starts_on, review_on)`` 投影日程
  （rest 槽不生成、``review_on`` 当日不生成、完成与否不影响推进），业务版本恰好 +1。
- 替换与归档：旧版本只保留历史（不重激活、不物理删除），只取消旧版**未来未锁定**日程；
  到期锁定按固定业务时区的业务日期规则判定，存储锁定标记同样视为已锁定。
- 受限组合：档案补丁与计划在同一事务内确认（先校验补丁，再按补丁后条件复查计划与频率／
  时长上限），一次确认只推进一次 ``context_version``；任一步失败全部回滚。
- 幂等：重复确认返回原凭据、不重复建版本／日程；关闭重开仍返回同一份凭据。
- 并发：同一草稿并发确认只生效一次；同一基线的两个草稿并发确认恰好一个生效。
- 「今天只能用…」不进长期档案：当次条件不是长期补丁，计划确认也不改写正式档案。

边界：全部经内部应用层（``PlanDraftService``／``DraftService``／``ConfirmService``）与
``tmp_path`` 临时文件库，不接 HTTP、不触碰真实用户数据目录。测试里对 ``scheduled_sessions``
的原始 UPDATE 只用于造出「已存储锁定」这一阶段外的存储状态替身（记录侧归 S3-09 起），
不是生产写入旁路。
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.confirm import (
    ConfirmService,
    DraftDiscarded,
    DraftRevisionConflict,
    DraftStale,
    PlanCommitResult,
)
from app.drafts import DraftKindMismatch, DraftService, UnknownDraft
from app.plan_drafts import PlanDraftService
from domain.plan.repo import PlanRepo
from domain.plan.rules import InvalidPlanPayload, project_sessions
from domain.profile.repo import ProfileRepo
from domain.profile.rules import apply_patch
from domain.profile.schema import Fact, Profile, ProfilePatch, SessionConditions
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage3_plan_drafts import REVIEW_ON, STARTS_ON, _payload, _profile

CONVERSATION_ID = "c1"
FULL_EQUIPMENT = ("杠铃", "哑铃", "绳索", "引体架")
# 替换场景的业务日期：窗口内第 5 个训练日（含）之前都已到期锁定，之后仍未锁定。
REPLACE_BUSINESS_DATE = date(2026, 9, 23)
# 旧版全部投影日程（PPL 每周三练，4 周窗口；rest 槽与 review_on 当日不生成）。
OLD_SCHEDULED_ON = (
    date(2026, 9, 14),
    date(2026, 9, 16),
    date(2026, 9, 18),
    date(2026, 9, 21),
    date(2026, 9, 23),
    date(2026, 9, 25),
    date(2026, 9, 28),
    date(2026, 9, 30),
    date(2026, 10, 2),
    date(2026, 10, 5),
    date(2026, 10, 7),
    date(2026, 10, 9),
)
# 造出「已存储锁定」的旧日程（已确认完成／漏练的存储标记替身，记录侧归 S3-09 起）。
STORED_LOCKED_ON = date(2026, 9, 28)


# ---------- 原始行读取与阶段外存储状态替身 ----------


async def _context_version(db: Database) -> int:
    return (await ProfileRepo(db).read()).context_version


async def _formal_profile_json(db: Database) -> str | None:
    """正式档案原始文本：验证「无补丁的计划确认不改写档案」需要逐字节比较。"""

    async def op(conn):
        async with conn.execute(
            "SELECT profile_json FROM user_profile WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return None if row["profile_json"] is None else str(row["profile_json"])

    return await db.under_lock(op)


async def _plan_rows(db: Database) -> dict[str, dict[str, object]]:
    async def op(conn):
        async with conn.execute(
            "SELECT id, version, source_plan_version_id, starts_on, review_on, mode,"
            " payload_json, source_draft_id, confirmed_at FROM plan_versions"
            " ORDER BY version"
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            str(row["id"]): {
                "version": int(row["version"]),
                "source_plan_version_id": row["source_plan_version_id"],
                "starts_on": str(row["starts_on"]),
                "review_on": str(row["review_on"]),
                "mode": str(row["mode"]),
                "payload_json": str(row["payload_json"]),
                "source_draft_id": str(row["source_draft_id"]),
                "confirmed_at": str(row["confirmed_at"]),
            }
            for row in rows
        }

    return await db.under_lock(op)


async def _session_rows(db: Database) -> dict[str, dict[str, object]]:
    async def op(conn):
        async with conn.execute(
            "SELECT id, plan_version_id, plan_workout_key, scheduled_on, cancelled_at,"
            " locked_at FROM scheduled_sessions ORDER BY scheduled_on, id"
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            str(row["id"]): {
                "plan_version_id": str(row["plan_version_id"]),
                "plan_workout_key": str(row["plan_workout_key"]),
                "scheduled_on": str(row["scheduled_on"]),
                "cancelled_at": row["cancelled_at"],
                "locked_at": row["locked_at"],
            }
            for row in rows
        }

    return await db.under_lock(op)


async def _mark_stored_locked(db: Database, *, plan_version_id: str, on: date) -> str:
    """把某条旧日程标记为「已存储锁定」（完成／漏练标记的阶段外替身），返回其 id。"""
    async with db.transaction() as conn:
        async with conn.execute(
            "SELECT id FROM scheduled_sessions WHERE plan_version_id = ?"
            " AND scheduled_on = ?",
            (plan_version_id, on.isoformat()),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None, (plan_version_id, on)
        session_id = str(row["id"])
        await conn.execute(
            "UPDATE scheduled_sessions SET locked_at = ? WHERE id = ?",
            ("2026-09-28T00:00:00+00:00", session_id),
        )
    return session_id


async def _draft_row(db: Database, draft_id: str) -> dict[str, object]:
    async def op(conn):
        async with conn.execute(
            "SELECT status, revision, committed_revision, committed_business_version"
            " FROM business_drafts WHERE id = ?",
            (draft_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None, draft_id
        return {
            "status": str(row["status"]),
            "revision": int(row["revision"]),
            "committed_revision": row["committed_revision"],
            "committed_business_version": row["committed_business_version"],
        }

    return await db.under_lock(op)


def _cancelled(
    rows: dict[str, dict[str, object]], plan_version_id: str
) -> dict[date, bool]:
    """某版本的应训练日 → 是否已取消。"""
    return {
        date.fromisoformat(str(row["scheduled_on"])): row["cancelled_at"] is not None
        for row in rows.values()
        if row["plan_version_id"] == plan_version_id
    }


# ---------- 准备：正式档案与 Pending 计划草稿 ----------


async def _formal_profile(db: Database, profile: Profile | None = None) -> None:
    """经真实档案确认链路建立正式档案（不旁路）：一次确认推进到 ``context_version=1``。"""
    await RunRepo(db).create_conversation(CONVERSATION_ID)
    drafts = DraftService(db)
    baseline = await drafts.prepare_generation_baseline()
    await drafts.create_profile_draft(
        draft_id="profile-draft-1",
        generation_baseline=baseline,
        conversation_id=CONVERSATION_ID,
        run_id=None,
        proposed=profile if profile is not None else _profile(),
    )
    await ConfirmService(db).confirm_profile_draft(
        draft_id="profile-draft-1", seen_revision=1
    )


async def _create_plan_draft(
    db: Database,
    *,
    draft_id: str,
    patch: ProfilePatch | None = None,
    business_date: date = STARTS_ON,
):
    """按准备快照创建一条 Pending 计划草稿；``patch`` 为受限组合的拟议长期补丁。"""
    service = PlanDraftService(db)
    preparation = await service.prepare_generation_input()
    payload = await _payload(db, _profile())
    return await service.create_plan_draft(
        draft_id=draft_id,
        preparation=preparation,
        conversation_id=CONVERSATION_ID,
        run_id=None,
        payload=payload,
        starts_on=STARTS_ON,
        review_on=REVIEW_ON,
        business_date=business_date,
        patch=patch,
    )


async def _confirm_plan(
    db: Database, *, draft_id: str, seen_revision: int = 1, business_date: date
) -> PlanCommitResult:
    return await ConfirmService(db).confirm_plan_draft(
        draft_id=draft_id, seen_revision=seen_revision, business_date=business_date
    )


# ---------- 启用 v1：投影日程与恰好 +1 ----------


async def test_first_plan_confirmation_activates_v1_with_projected_sessions(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        before_profile = await _formal_profile_json(db)

        result = await _confirm_plan(
            db, draft_id="plan-draft-1", business_date=STARTS_ON
        )

        # 凭据：草稿、所见 revision、该次提交后的业务版本，以及该次建立的计划版本
        assert result.draft_id == "plan-draft-1"
        assert result.committed_revision == 1
        assert result.committed_business_version == 2  # 档案确认的 1 + 本次恰好 +1
        assert result.plan_version == 1

        plans = await _plan_rows(db)
        assert len(plans) == 1
        row = plans[result.plan_version_id]
        assert row["version"] == 1
        assert row["source_plan_version_id"] is None  # 首个计划没有来源版本
        assert row["starts_on"] == STARTS_ON.isoformat()
        assert row["review_on"] == REVIEW_ON.isoformat()
        assert row["mode"] == "regular"
        assert row["source_draft_id"] == "plan-draft-1"
        assert row["confirmed_at"]

        # 投影日程：只含 workout 日（rest 槽与 review_on 当日不生成），全部未取消未锁定
        sessions = await _session_rows(db)
        assert {
            date.fromisoformat(str(item["scheduled_on"])) for item in sessions.values()
        } == set(OLD_SCHEDULED_ON)
        for item in sessions.values():
            assert item["plan_version_id"] == result.plan_version_id
            assert item["cancelled_at"] is None
            assert item["locked_at"] is None
            assert item["plan_workout_key"] in {"push", "pull", "legs"}

        # 业务版本恰好 +1；无补丁的组合不改正式档案
        assert await _context_version(db) == 2
        assert await _formal_profile_json(db) == before_profile

        draft = await _draft_row(db, "plan-draft-1")
        assert draft["status"] == "committed"
        assert draft["committed_revision"] == 1
        assert draft["committed_business_version"] == 2


async def test_projected_sessions_match_the_domain_projection_exactly(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        view = await _create_plan_draft(db, draft_id="plan-draft-1")
        await _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON)

        expected = project_sessions(
            view.proposal.payload,
            starts_on=view.proposal.starts_on,
            review_on=view.proposal.review_on,
        )
        sessions = await _session_rows(db)
        actual = sorted(
            (
                date.fromisoformat(str(item["scheduled_on"])),
                str(item["plan_workout_key"]),
            )
            for item in sessions.values()
        )
        assert actual == sorted(
            (item.scheduled_on, item.plan_workout_key) for item in expected
        )
        assert len(actual) == len(OLD_SCHEDULED_ON)


# ---------- 替换：归档旧版、只取消未来未锁定日程 ----------


async def test_replacement_archives_old_version_and_cancels_only_future_unlocked(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        first = await _confirm_plan(
            db, draft_id="plan-draft-1", business_date=STARTS_ON
        )
        stored_locked_id = await _mark_stored_locked(
            db, plan_version_id=first.plan_version_id, on=STORED_LOCKED_ON
        )
        first_rows = await _plan_rows(db)

        # 替换草稿按生成读取时刻绑定来源版本（= 当刻正式计划）
        await _create_plan_draft(db, draft_id="plan-draft-2")
        second = await _confirm_plan(
            db, draft_id="plan-draft-2", business_date=REPLACE_BUSINESS_DATE
        )

        plans = await _plan_rows(db)
        assert len(plans) == 2
        assert second.plan_version == 2
        # 归档 = 保留历史：旧版本行逐字段不变（不重激活、不物理删除）
        assert plans[first.plan_version_id] == first_rows[first.plan_version_id]
        assert plans[second.plan_version_id]["version"] == 2
        assert plans[second.plan_version_id]["source_plan_version_id"] == (
            first.plan_version_id
        )

        sessions = await _session_rows(db)
        old = _cancelled(sessions, first.plan_version_id)
        new = _cancelled(sessions, second.plan_version_id)
        assert set(new) == set(OLD_SCHEDULED_ON)
        assert all(not cancelled for cancelled in new.values())
        # 到期（业务日期 >= 应训练日）与已存储锁定的旧日程都保留；只有未来未锁定的被取消
        assert old == {
            on: on > REPLACE_BUSINESS_DATE and on != STORED_LOCKED_ON
            for on in OLD_SCHEDULED_ON
        }
        assert sessions[stored_locked_id]["locked_at"] is not None
        assert sessions[stored_locked_id]["cancelled_at"] is None

        plans_repo = PlanRepo(db)
        archived = await plans_repo.read_version(first.plan_version_id)
        current = await plans_repo.read_current()
        assert archived is not None
        assert current is not None and current.id == second.plan_version_id
        assert await _context_version(db) == 3


# ---------- 受限组合：补丁与计划同一事务 ----------


async def test_combination_patch_is_applied_in_the_same_transaction(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        baseline = (await ProfileRepo(db).read()).profile
        assert baseline is not None
        patch = ProfilePatch(
            facts={
                "session_duration_minutes": Fact.known(75),
                "training_goal": Fact.known("力量"),
            }
        )
        await _create_plan_draft(db, draft_id="plan-draft-1", patch=patch)

        result = await _confirm_plan(
            db, draft_id="plan-draft-1", business_date=STARTS_ON
        )

        official = (await ProfileRepo(db).read()).profile
        assert official == apply_patch(baseline, patch)
        assert official != baseline
        # 组合只推进一次业务版本，计划版本同事务建立
        assert result.committed_business_version == 2
        assert await _context_version(db) == 2
        assert len(await _plan_rows(db)) == 1


async def test_patch_lowered_limits_are_rechecked_against_the_plan(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        before_profile = await _formal_profile_json(db)
        # 生成用的是原档案（60 分钟／每周 3 次），补丁把上限压到计划之下：确认时必须按
        # 补丁后条件复查出来（「不能只按旧条件校验」）
        await _create_plan_draft(
            db,
            draft_id="plan-draft-freq",
            patch=ProfilePatch(facts={"weekly_frequency": Fact.known(2)}),
        )
        await _create_plan_draft(
            db,
            draft_id="plan-draft-dur",
            patch=ProfilePatch(facts={"session_duration_minutes": Fact.known(10)}),
        )

        for draft_id, keyword in (
            ("plan-draft-freq", "超过档案每周频率"),
            ("plan-draft-dur", "超过档案单次可用时长"),
        ):
            with pytest.raises(InvalidPlanPayload, match=keyword):
                await _confirm_plan(db, draft_id=draft_id, business_date=STARTS_ON)
            assert (await _draft_row(db, draft_id))["status"] == "pending"

        # 零写入：没有计划版本、没有日程、档案与业务版本原样
        assert await _plan_rows(db) == {}
        assert await _session_rows(db) == {}
        assert await _formal_profile_json(db) == before_profile
        assert await _context_version(db) == 1


async def test_today_only_conditions_never_become_a_long_term_patch(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        # 「今天只能用…」是当次条件（SessionConditions），不是长期补丁：结构上不能作为补丁
        # 落库，草稿也不会被创建
        with pytest.raises(TypeError, match="不进入长期补丁"):
            await _create_plan_draft(
                db,
                draft_id="plan-draft-today",
                patch=SessionConditions(  # type: ignore[arg-type]
                    available_equipment=Fact.known(("哑铃",))
                ),
            )
        assert (await PlanDraftService(db).get_plan_draft("plan-draft-today")) is None
        assert await _plan_rows(db) == {}

        # 计划确认（无补丁）不改写长期档案：只有业务版本推进
        before_profile = await _formal_profile_json(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        await _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON)
        assert await _formal_profile_json(db) == before_profile
        assert await _context_version(db) == 2


# ---------- 失败注入：任一步失败全部回滚 ----------


async def test_injected_failure_rolls_back_plan_profile_version_and_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    # 不用 pytest.mark.parametrize：anyio 插件为 async 测试重建 callspec 时会丢弃参数化
    # （tests/test_provider_settings.py 已记录）。每种注入点用独立临时库。
    cases = (
        ("domain.plan.repo", "PlanRepo", "insert_sessions_in_transaction"),
        ("domain.plan.repo", "PlanRepo", "append_version_in_transaction"),
        ("domain.profile.repo", "ProfileRepo", "bump_context_version_in_transaction"),
    )
    for index, (module_name, class_name, method) in enumerate(cases):
        with monkeypatch.context() as patch_scope:
            async with open_database(tmp_path / f"app-{index}.db") as db:
                await _formal_profile(db)
                before_profile = await _formal_profile_json(db)
                await _create_plan_draft(
                    db,
                    draft_id="plan-draft-1",
                    patch=ProfilePatch(
                        facts={"session_duration_minutes": Fact.known(75)}
                    ),
                )

                owner = getattr(importlib.import_module(module_name), class_name)

                async def boom(*args: object, **kwargs: object) -> None:
                    raise RuntimeError("注入失败：验证整体回滚")

                patch_scope.setattr(owner, method, boom)
                with pytest.raises(RuntimeError, match="注入失败"):
                    await _confirm_plan(
                        db, draft_id="plan-draft-1", business_date=STARTS_ON
                    )

                # 计划、日程、档案补丁、业务版本、草稿状态与凭据全部回滚
                assert await _plan_rows(db) == {}
                assert await _session_rows(db) == {}
                assert await _formal_profile_json(db) == before_profile
                assert await _context_version(db) == 1
                draft = await _draft_row(db, "plan-draft-1")
                assert draft["status"] == "pending"
                assert draft["committed_revision"] is None
                assert draft["committed_business_version"] is None


async def test_failure_in_the_middle_of_replacement_keeps_old_sessions_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        first = await _confirm_plan(
            db, draft_id="plan-draft-1", business_date=STARTS_ON
        )
        await _create_plan_draft(db, draft_id="plan-draft-2")

        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("注入失败：替换回滚")

        monkeypatch.setattr(PlanRepo, "insert_sessions_in_transaction", boom)
        with pytest.raises(RuntimeError, match="注入失败"):
            await _confirm_plan(
                db, draft_id="plan-draft-2", business_date=REPLACE_BUSINESS_DATE
            )

        # 取消与版本追加同事务回滚：旧版日程一条也没被取消，当前计划仍是 v1
        assert set(await _plan_rows(db)) == {first.plan_version_id}
        old = _cancelled(await _session_rows(db), first.plan_version_id)
        assert set(old) == set(OLD_SCHEDULED_ON)
        assert not any(old.values())
        assert await _context_version(db) == 2


# ---------- 幂等与重开 ----------


async def test_repeat_confirmation_returns_the_same_receipt_without_new_writes(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        first = await _confirm_plan(
            db, draft_id="plan-draft-1", business_date=STARTS_ON
        )
        plans = await _plan_rows(db)
        sessions = await _session_rows(db)

        # 旧 seen_revision 与后来的业务日期都不改变幂等返回
        again = await _confirm_plan(
            db,
            draft_id="plan-draft-1",
            seen_revision=99,
            business_date=REVIEW_ON,
        )
        assert again == first
        assert await _plan_rows(db) == plans
        assert await _session_rows(db) == sessions
        assert await _context_version(db) == 2

    # 关闭重开后凭据仍在：不新建版本或日程，返回同一份结果
    async with open_database(tmp_path / "app.db") as reopened:
        reopened_result = await _confirm_plan(
            reopened, draft_id="plan-draft-1", business_date=STARTS_ON
        )
        assert reopened_result == first
        assert len(await _plan_rows(reopened)) == 1
        assert len(await _session_rows(reopened)) == len(OLD_SCHEDULED_ON)
        assert await _context_version(reopened) == 2


# ---------- 并发：恰好一次生效 ----------


async def test_concurrent_confirmation_of_one_draft_writes_exactly_once(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")

        results = await asyncio.gather(
            _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON),
            _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON),
        )

        assert results[0] == results[1]
        assert len(await _plan_rows(db)) == 1
        assert len(await _session_rows(db)) == len(OLD_SCHEDULED_ON)
        assert await _context_version(db) == 2


async def test_concurrent_confirmation_of_two_drafts_applies_exactly_one(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        await _create_plan_draft(db, draft_id="plan-draft-2")

        results = await asyncio.gather(
            _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON),
            _confirm_plan(db, draft_id="plan-draft-2", business_date=STARTS_ON),
            return_exceptions=True,
        )

        succeeded = [item for item in results if isinstance(item, PlanCommitResult)]
        stale = [item for item in results if isinstance(item, DraftStale)]
        assert len(succeeded) == 1
        assert len(stale) == 1
        assert set(await _plan_rows(db)) == {succeeded[0].plan_version_id}
        assert await _context_version(db) == 2
        # 过期草稿保持 Pending，未被改写、也不产生第二套日程
        loser = (
            "plan-draft-2"
            if succeeded[0].draft_id == "plan-draft-1"
            else "plan-draft-1"
        )
        assert (await _draft_row(db, loser))["status"] == "pending"
        assert len(await _session_rows(db)) == len(OLD_SCHEDULED_ON)


# ---------- 拦截与守卫：零写入 ----------


async def test_stale_baseline_and_old_revision_are_refused_without_writes(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        await _create_plan_draft(db, draft_id="plan-draft-2")
        await _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON)
        plans = await _plan_rows(db)
        sessions = await _session_rows(db)

        # 基线过期：另一草稿已确认并推进业务版本
        with pytest.raises(DraftStale):
            await _confirm_plan(db, draft_id="plan-draft-2", business_date=STARTS_ON)

        # 旧 revision：同一草稿纠错后不得用旧所见版本确认
        await _create_plan_draft(db, draft_id="plan-draft-3")
        service = PlanDraftService(db)
        view = await service.get_plan_draft("plan-draft-3")
        assert view is not None
        await service.revise_plan_draft(
            draft_id="plan-draft-3",
            seen_revision=1,
            payload=view.proposal.payload,
            starts_on=STARTS_ON,
            review_on=REVIEW_ON,
        )
        with pytest.raises(DraftRevisionConflict):
            await _confirm_plan(
                db, draft_id="plan-draft-3", seen_revision=1, business_date=STARTS_ON
            )

        assert await _plan_rows(db) == plans
        assert await _session_rows(db) == sessions
        assert await _context_version(db) == 2
        for draft_id in ("plan-draft-2", "plan-draft-3"):
            assert (await _draft_row(db, draft_id))["status"] == "pending"


async def test_kind_unknown_and_discarded_guards(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        drafts = DraftService(db)
        confirms = ConfirmService(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        await _create_plan_draft(db, draft_id="plan-draft-3")
        await PlanDraftService(db).discard_plan_draft(draft_id="plan-draft-3")
        baseline = await drafts.prepare_generation_baseline()
        await drafts.create_profile_draft(
            draft_id="profile-draft-2",
            generation_baseline=baseline,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            proposed=_profile(),
        )

        with pytest.raises(UnknownDraft):
            await confirms.confirm_plan_draft(
                draft_id="missing", seen_revision=1, business_date=STARTS_ON
            )
        with pytest.raises(DraftKindMismatch):
            await confirms.confirm_plan_draft(
                draft_id="profile-draft-2",
                seen_revision=1,
                business_date=STARTS_ON,
            )
        with pytest.raises(DraftKindMismatch):
            await confirms.confirm_profile_draft(
                draft_id="plan-draft-1", seen_revision=1
            )
        with pytest.raises(DraftDiscarded):
            await confirms.confirm_plan_draft(
                draft_id="plan-draft-3",
                seen_revision=1,
                business_date=STARTS_ON,
            )
        assert await _plan_rows(db) == {}
        assert await _context_version(db) == 1


async def test_plan_draft_without_formal_profile_is_refused(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        # 未建档：补丁可以提供器械与身体情况，但不能代替首次建档（缺档案 fail-closed）
        draft = await _create_plan_draft(
            db,
            draft_id="plan-draft-1",
            patch=ProfilePatch(
                facts={
                    "available_equipment": Fact.known(FULL_EQUIPMENT),
                    "body_conditions": Fact.denied(),
                }
            ),
        )
        assert draft.proposed_profile is not None

        with pytest.raises(InvalidPlanPayload, match="尚未建立正式档案"):
            await _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON)

        assert await _plan_rows(db) == {}
        assert await _session_rows(db) == {}
        assert await _formal_profile_json(db) is None
        assert await _context_version(db) == 0
        assert (await _draft_row(db, "plan-draft-1"))["status"] == "pending"


async def test_source_version_divergence_from_current_plan_is_refused(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        first = await _confirm_plan(
            db, draft_id="plan-draft-1", business_date=STARTS_ON
        )
        await _create_plan_draft(db, draft_id="plan-draft-2")

        # 库外改写正式事实：出现一个更「新」的计划版本，来源版本不再等于当刻正式计划
        async def op(conn):
            await conn.execute(
                "INSERT INTO plan_versions (id, version, source_plan_version_id,"
                " starts_on, review_on, mode, payload_json, source_draft_id,"
                " confirmed_at) SELECT 'rogue', 99, NULL, starts_on, review_on, mode,"
                " payload_json, source_draft_id, confirmed_at FROM plan_versions"
                " WHERE id = ?",
                (first.plan_version_id,),
            )

        await db.under_lock(op)

        with pytest.raises(InvalidPlanPayload, match="不一致"):
            await _confirm_plan(db, draft_id="plan-draft-2", business_date=STARTS_ON)

        # 拒绝后草稿仍 Pending、没有新版本、旧版日程也未被取消
        assert (await _draft_row(db, "plan-draft-2"))["status"] == "pending"
        assert len(await _plan_rows(db)) == 2
        assert await _context_version(db) == 2
        assert not any(
            _cancelled(await _session_rows(db), first.plan_version_id).values()
        )
