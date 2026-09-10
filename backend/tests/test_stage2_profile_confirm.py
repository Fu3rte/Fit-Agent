"""Stage 2 S2-05：档案确认事务、唯一版本推进、持久化幂等与原子回滚。

验收对照（stage2.md §4.2、§5 S2-05、§8 已拍方案 A）：

- §4.2 顺序在唯一连接／唯一锁／同一事务内逐步执行：已 Committed 直接返回保存的原提交结果
  （不重查旧业务版本或旧 revision、不重复写入）；拒绝 Discarded、不存在的身份明确失败且不
  创建新草稿；读当前档案与 ``context_version`` 后严格检查业务基线（版本号）与所见 revision；
  对库内保存的最终草稿内容做事务内领域复查（结构、动作／模式引用、首次建档完整性）；
  写正式档案 → ``context_version`` 恰好 +1 → 草稿 Committed 与不可变凭据；提交后才返回。
- §8 已拍方案 A：最终草稿与正式档案全部相同时拒绝提交、版本不变、草稿保持 Pending。
- 幂等与恢复：双击／并发确认、响应丢失重试、后续业务版本变化后重试、关闭重开后重试都返回
  同一原结果，且正式副作用只有一次（一次档案写入、一次 +1）。
- 原子性与并发：分别在档案写入后、版本更新后、草稿／凭据写入后且 COMMIT 前注入失败，以及
  事务中被取消，全部整体回滚（档案、版本、草稿行与凭据原样）且无锁泄漏；确认与确认并发只
  产生一次生效。

边界：不实现过期变更项计算与 409 映射（S2-06）、HTTP 传输校验与错误码（S2-07）、重算
（Stage 4）。用 pytest ``tmp_path`` 下的临时文件库，不触碰真实用户数据目录、不联网；
非 Windows 平台结果不作为 Windows 阶段门槛（stage2.md §6）。
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.confirm import (
    ConfirmService,
    DraftDiscarded,
    DraftStale,
    NoBusinessChange,
    ProfileCommitResult,
)
from app.draft_repo import INITIAL_REVISION, DraftRepo
from app.drafts import (
    DraftRevisionConflict,
    DraftService,
    UnknownDraft,
)
from domain.profile.repo import ProfileRepo
from domain.profile.rules import (
    IncompleteProfile,
    InvalidProfile,
    UnknownExerciseReference,
)
from domain.profile.schema import (
    FACT_FIELDS,
    ActionRestriction,
    Fact,
    InvalidProfileRow,
    Profile,
    ProfileSnapshot,
    profile_to_json,
)
from domain.profile.service import ProfileService
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database

SEEDED_EXERCISE_ID = "barbell-back-squat"  # 003 迁移种子：合法具体动作身份


def _first_time_profile() -> Profile:
    """首次建档样本：九项明确回答齐备（§4.3 已拍 1B，可过首次确认完整性门）。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.known("零基础"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.known(("哑铃",)),
        action_restrictions=Fact.denied(),
        body_conditions=Fact.denied(),
        body_weight_kg=Fact.known(73.0),
    )


def _existing_profile() -> Profile:
    """已建档正式档案样本（非首次确认对照）。"""
    return replace(
        _first_time_profile(),
        training_goal=Fact.known("力量"),
        training_experience=Fact.known("进阶"),
        body_weight_kg=Fact.known(80.0),
    )


def _changed_profile() -> Profile:
    """相对 :func:`_existing_profile` 确有业务变化的拟议内容。"""
    return replace(_existing_profile(), body_weight_kg=Fact.known(79.0))


# 绕过创建期校验塞进库里的最终草稿内容（模拟库外改写／损坏的草稿行）：
# 确认事务必须自己复查保存内容，而不是相信创建时校验过一次。
TAMPER_CASES = (
    (
        "raw-invalid",
        "{}",
        InvalidProfileRow,
        "缺字段",
    ),
    (
        "unknown-mode",
        profile_to_json(
            replace(
                _first_time_profile(),
                action_restrictions=Fact.known(
                    (ActionRestriction(scope="movement_pattern", target="太极"),)
                ),
            )
        ),
        InvalidProfile,
        "非法限制",
    ),
    (
        "unknown-action",
        profile_to_json(
            replace(
                _first_time_profile(),
                action_restrictions=Fact.known(
                    (
                        ActionRestriction(
                            scope="specific_action", target="no-such-action"
                        ),
                    )
                ),
            )
        ),
        UnknownExerciseReference,
        "no-such-action",
    ),
)


async def _write_formal_profile(db: Database, profile: Profile) -> None:
    """测试替身：在确认事务之外直接写正式档案并推进一次版本。

    只用于构造「已有正式档案」与「生成后又发生一次正式提交」两种前置状态；生产上正式档案
    写入与版本推进只在 :class:`ConfirmService` 的确认事务内发生。
    """
    async with db.transaction() as conn:
        await ProfileService(db).write_profile_in_transaction(conn, profile)
        await ProfileRepo(db).bump_context_version_in_transaction(conn)


async def _tamper_stored_proposal(db: Database, draft_id: str, payload: str) -> None:
    """把库内最终草稿内容直接改成 ``payload``（不经过创建／纠错校验）。"""
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE business_drafts SET proposed_profile_json = ? WHERE id = ?",
            (payload, draft_id),
        )


def _legacy_nine_field_json() -> str:
    """旧版九字段 profile_json（身体情况尚未合并）；只用于构造存量档案前置状态。"""
    payload = json.loads(profile_to_json(_existing_profile()))
    del payload["body_conditions"]
    payload["body_state"] = {"state": "known", "value": ["肩部偶有不适"]}
    payload["red_flags"] = {"state": "known", "value": ["肩部偶有不适", "锐痛"]}
    return json.dumps(payload, ensure_ascii=False)


async def _write_legacy_formal_profile_json(db: Database, payload: str) -> None:
    """直接写入旧格式 profile_json 并推进一次版本：模拟身体情况合并前已有的存量档案。"""
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE user_profile SET profile_json = ? WHERE id = 1", (payload,)
        )
        await ProfileRepo(db).bump_context_version_in_transaction(conn)


async def _stored_profile_json(db: Database) -> str:
    async def op(conn):
        async with conn.execute(
            "SELECT profile_json FROM user_profile WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return row["profile_json"]

    return await db.under_lock(op)


async def _setup_draft(
    db: Database,
    *,
    formal: Profile | None,
    proposed: Profile,
    draft_id: str = "d1",
) -> tuple[DraftService, ProfileSnapshot]:
    """准备来源会话、正式档案（``None`` = 未建档）与一条 Pending 草稿。

    返回草稿服务与草稿的生成基线（``context_version`` 与档案同一快照）。
    """
    await RunRepo(db).create_conversation("c1")
    await RunRepo(db).create_run_with_user_message("c1", "r1", "cri-1", "帮我建档")
    if formal is not None:
        await _write_formal_profile(db, formal)
    drafts = DraftService(db)
    baseline = await drafts.prepare_generation_baseline()
    await drafts.create_profile_draft(
        draft_id=draft_id,
        generation_baseline=baseline,
        conversation_id="c1",
        run_id="r1",
        proposed=proposed,
    )
    return drafts, baseline


async def _pending_view(drafts: DraftService, draft_id: str = "d1"):
    view = await drafts.get_draft(draft_id)
    assert view is not None
    assert view.draft.status == "pending"
    return view


# ---------- 首次确认：正式落盘、一次 +1、凭据不可变 ----------


async def test_first_confirmation_builds_profile_and_bumps_version_exactly_once(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        assert baseline.profile is None and baseline.context_version == 0
        before = await _pending_view(drafts)

        result = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=before.draft.revision
        )

        # 返回该次提交的不可变结果：草稿身份＋已提交 revision＋提交后的业务版本
        assert result == ProfileCommitResult(
            draft_id="d1",
            committed_revision=INITIAL_REVISION,
            committed_business_version=baseline.context_version + 1,
        )
        # 正式档案只在确认后生效，统一业务版本恰好 +1
        formal = await ProfileRepo(db).read()
        assert formal.profile == _first_time_profile()
        assert formal.context_version == 1
        # 草稿 Committed，凭据与草稿行同一次事务写入
        committed = await drafts.get_draft("d1")
        assert committed is not None
        assert committed.draft.status == "committed"
        assert (
            committed.draft.committed_revision,
            committed.draft.committed_business_version,
        ) == (INITIAL_REVISION, formal.context_version)


async def test_confirmation_of_an_existing_profile_applies_the_proposed_change(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        _drafts, baseline = await _setup_draft(
            db, formal=_existing_profile(), proposed=_changed_profile()
        )
        assert baseline.context_version == 1

        result = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION
        )

        assert result.committed_business_version == 2
        formal = await ProfileRepo(db).read()
        assert formal.profile == _changed_profile()
        assert formal.context_version == 2


async def test_legacy_stored_profile_is_read_then_confirmed_in_the_new_format(
    tmp_path: Path,
) -> None:
    """旧存量档案：读取时合并身体情况 → 可生成并确认新格式草稿 → 落盘为八字段结构。"""
    async with open_database(tmp_path / "app.db") as db:
        await _write_legacy_formal_profile_json(db, _legacy_nine_field_json())

        # 读取旧 JSON：两项旧身体情况合并为 body_conditions（保序去重）
        snapshot = await ProfileRepo(db).read()
        assert snapshot.context_version == 1
        assert snapshot.profile is not None
        assert snapshot.profile.body_conditions == Fact.known(("肩部偶有不适", "锐痛"))

        await RunRepo(db).create_conversation("c1")
        await RunRepo(db).create_run_with_user_message(
            "c1", "r1", "cri-1", "改一下体重"
        )
        drafts = DraftService(db)
        baseline = await drafts.prepare_generation_baseline()
        assert baseline.context_version == 1
        await drafts.create_profile_draft(
            draft_id="d1",
            generation_baseline=baseline,
            conversation_id="c1",
            run_id="r1",
            proposed=replace(
                _changed_profile(),
                body_conditions=Fact.known(("肩部偶有不适", "锐痛")),
            ),
        )

        result = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=INITIAL_REVISION
        )

        assert result.committed_business_version == 2
        payload = json.loads(await _stored_profile_json(db))
        # 写入只产生新版八字段结构；确认事务仍恰好推进一次版本
        assert sorted(payload) == sorted(FACT_FIELDS)
        assert "body_state" not in payload and "red_flags" not in payload
        assert payload["body_conditions"] == {
            "state": "known",
            "value": ["肩部偶有不适", "锐痛"],
        }
        assert (await ProfileRepo(db).read()).context_version == 2


# ---------- 幂等：重复确认始终返回原结果，正式副作用只有一次 ----------


async def test_repeat_confirmation_returns_the_same_result_without_rewriting(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        drafts, _baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        service = ConfirmService(db)
        first = await service.confirm_profile_draft(draft_id="d1", seen_revision=1)
        committed = await drafts.get_draft("d1")

        second = await service.confirm_profile_draft(draft_id="d1", seen_revision=1)

        # 重复确认返回同一份原结果，不重复写入（草稿行逐字段一致）
        assert second == first
        assert await drafts.get_draft("d1") == committed
        formal = await ProfileRepo(db).read()
        assert formal.context_version == 1
        assert formal.profile == _first_time_profile()


async def test_repeat_confirmation_ignores_later_versions_and_stale_revisions(
    tmp_path: Path,
) -> None:
    """后续版本已变化、且请求带旧 revision：幂等返回原凭据，不重查也不改写正式数据。"""
    async with open_database(tmp_path / "app.db") as db:
        drafts, _baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        service = ConfirmService(db)
        original = await service.confirm_profile_draft(draft_id="d1", seen_revision=1)

        # 提交之后业务又前进一步（另一条正式提交把版本推到 2 并改写档案）
        await _write_formal_profile(db, _existing_profile())

        retried = await service.confirm_profile_draft(draft_id="d1", seen_revision=99)

        assert retried == original  # 原提交结果不被后续业务变化改写
        assert retried.committed_business_version == 1
        formal = await ProfileRepo(db).read()
        assert formal.context_version == 2
        assert formal.profile == _existing_profile()  # 重试不重复写入正式档案
        assert await drafts.get_draft("d1") is not None


async def test_lost_response_is_recovered_by_repeating_the_confirmation(
    tmp_path: Path,
) -> None:
    """提交成功但响应丢失：不假定连接断开代表未提交，重复确认即找回原结果。"""
    async with open_database(tmp_path / "app.db") as db:
        _drafts, _baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        await ConfirmService(db).confirm_profile_draft(draft_id="d1", seen_revision=1)

        # 客户端超时后重发：拿回同一凭据，正式数据不被二次推进
        recovered = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=1
        )

        assert recovered == ProfileCommitResult(
            draft_id="d1", committed_revision=1, committed_business_version=1
        )
        assert (await ProfileRepo(db).read()).context_version == 1


async def test_repeat_confirmation_after_close_and_reopen_returns_the_original_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await _setup_draft(db, formal=None, proposed=_first_time_profile())
        original = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=1
        )

    async with open_database(path) as db:
        reloaded = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=1
        )

        assert reloaded == original
        assert (await ProfileRepo(db).read()).context_version == 1


async def test_concurrent_confirmation_has_a_single_effect_and_one_result(
    tmp_path: Path,
) -> None:
    """双击／并发确认：唯一锁下串行，两者拿到同一原结果，正式写入只有一次。"""
    async with open_database(tmp_path / "app.db") as db:
        await _setup_draft(db, formal=None, proposed=_first_time_profile())
        service = ConfirmService(db)

        results = await asyncio.wait_for(
            asyncio.gather(
                service.confirm_profile_draft(draft_id="d1", seen_revision=1),
                service.confirm_profile_draft(draft_id="d1", seen_revision=1),
            ),
            timeout=10,
        )

        assert results[0] == results[1]
        assert results[0].committed_business_version == 1
        assert (await ProfileRepo(db).read()).context_version == 1


# ---------- 拦截：未找到、已丢弃、过期、revision 不符、无业务变化 ----------


async def test_unknown_draft_identity_is_rejected_without_creating_a_draft(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )

        with pytest.raises(UnknownDraft, match="d-missing"):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d-missing", seen_revision=1
            )

        # 明确返回未找到，不创建新草稿、不写正式事实
        assert await drafts.get_draft("d-missing") is None
        assert len(await drafts.list_drafts("c1")) == 1
        assert await ProfileRepo(db).read() == baseline


async def test_discarded_draft_cannot_be_confirmed(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        discarded = await drafts.discard_draft(draft_id="d1")

        with pytest.raises(DraftDiscarded, match="已丢弃"):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d1", seen_revision=1
            )

        # 丢弃后不可提交：无正式写入、无版本推进、草稿仍是 Discarded 无凭据
        assert await ProfileRepo(db).read() == baseline
        assert await drafts.get_draft("d1") == discarded


async def test_stale_business_baseline_is_rejected_without_any_write(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_draft(
            db, formal=_existing_profile(), proposed=_changed_profile()
        )
        before = await _pending_view(drafts)
        # 生成期间发生一次正式提交：草稿基线 V=1 已过期（当前 V=2）
        await _write_formal_profile(
            db, replace(_existing_profile(), training_goal=Fact.known("耐力"))
        )
        current = await ProfileRepo(db).read()
        assert current.context_version == 2

        with pytest.raises(DraftStale, match="context_version"):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d1", seen_revision=1
            )

        # 过期只是基线冲突：不改变草稿状态／基线、不写正式数据
        assert await drafts.get_draft("d1") == before
        assert await ProfileRepo(db).read() == current
        assert before.draft.base_business_version == baseline.context_version


async def test_seen_revision_mismatch_is_rejected_without_any_write(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        revised = await drafts.revise_profile_draft(
            draft_id="d1",
            seen_revision=INITIAL_REVISION,
            proposed=replace(_first_time_profile(), training_goal=Fact.known("耐力")),
        )
        assert revised.draft.revision == INITIAL_REVISION + 1

        for stale in (INITIAL_REVISION, INITIAL_REVISION + 5):
            with pytest.raises(DraftRevisionConflict, match="revision"):
                await ConfirmService(db).confirm_profile_draft(
                    draft_id="d1", seen_revision=stale
                )
            assert await drafts.get_draft("d1") == revised
        assert await ProfileRepo(db).read() == baseline


async def test_no_business_change_is_rejected_and_the_draft_stays_pending(
    tmp_path: Path,
) -> None:
    """§8 已拍方案 A：最终草稿与正式档案全部相同 → 不提交、不 +1、保持 Pending。"""
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_draft(
            db, formal=_existing_profile(), proposed=_existing_profile()
        )
        before = await _pending_view(drafts)

        with pytest.raises(NoBusinessChange, match="无业务变更"):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d1", seen_revision=1
            )

        # 无业务变更不是成功：无凭据、版本不变、正式档案不变
        assert await ProfileRepo(db).read() == baseline
        assert await drafts.get_draft("d1") == before
        # 草稿仍可继续纠错或丢弃
        assert (await drafts.discard_draft(draft_id="d1")).draft.status == "discarded"
        assert await ProfileRepo(db).read() == baseline


async def test_first_time_confirmation_rejects_an_incomplete_draft_then_accepts_it(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_draft(
            db,
            formal=None,
            proposed=Profile(training_goal=Fact.known("增肌")),  # 其余八项未回答
        )
        before = await _pending_view(drafts)

        with pytest.raises(IncompleteProfile, match="首次建档"):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d1", seen_revision=1
            )

        # 不补造事实、不写正式档案、不推进版本，草稿保持 Pending 且 revision 不变
        assert await ProfileRepo(db).read() == baseline
        assert await drafts.get_draft("d1") == before

        # 补全后仍可确认（缺失是唯一拦截原因，不是永久拒绝）
        completed = await drafts.revise_profile_draft(
            draft_id="d1", seen_revision=1, proposed=_first_time_profile()
        )
        result = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=completed.draft.revision
        )
        assert result.committed_revision == completed.draft.revision
        assert (await ProfileRepo(db).read()).profile == _first_time_profile()


async def _assert_tampered_final_content_is_rejected(
    path: Path, draft_id: str, payload: str, expected_error: type[Exception], match: str
) -> None:
    """把库内最终内容改成非法值后确认：被拦下且正式数据与草稿行原样。"""
    async with open_database(path) as db:
        formal = await ProfileRepo(db).read()
        _drafts, _baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile(), draft_id=draft_id
        )
        await _tamper_stored_proposal(db, draft_id, payload)

        with pytest.raises(expected_error, match=match):
            await ConfirmService(db).confirm_profile_draft(
                draft_id=draft_id, seen_revision=1
            )

        # 非法最终内容不落库：正式档案、版本与草稿行原样（无凭据、仍 Pending）
        assert await ProfileRepo(db).read() == formal
        row = await DraftRepo(db).get(draft_id)
        assert row is not None and row.status == "pending"
        assert (row.committed_revision, row.committed_business_version) == (None, None)


async def test_confirmation_revalidates_the_stored_final_content(
    tmp_path: Path,
) -> None:
    """复查对象是数据库保存的内容：绕过创建校验塞进去的非法内容在确认时被拦下。"""
    for draft_id, payload, expected_error, match in TAMPER_CASES:
        await _assert_tampered_final_content_is_rejected(
            tmp_path / f"{draft_id}.db", draft_id, payload, expected_error, match
        )


async def test_confirmation_only_uses_the_transaction_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认事务内只用外层连接：调用自取锁入口会因锁不可重入而死锁，这里显式禁用它。"""
    async with open_database(tmp_path / "app.db") as db:
        _drafts, _baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        calls: list[str] = []

        async def forbidden(self, *args, **kwargs):
            calls.append(type(self).__name__)
            raise AssertionError("确认事务内不得调用自取锁入口")

        monkeypatch.setattr(ProfileRepo, "read", forbidden)
        monkeypatch.setattr(DraftRepo, "get", forbidden)
        result = await ConfirmService(db).confirm_profile_draft(
            draft_id="d1", seen_revision=1
        )
        monkeypatch.undo()

        assert calls == []
        assert result.committed_business_version == 1
        assert (await ProfileRepo(db).read()).context_version == 1


# ---------- 原子性：注入失败与取消后整体回滚、无锁泄漏 ----------


async def _assert_everything_rolled_back(
    db: Database,
    drafts: DraftService,
    *,
    before,
    baseline: ProfileSnapshot,
) -> None:
    """档案、业务版本与草稿行（含凭据）都保持确认前状态。"""
    assert await ProfileRepo(db).read() == baseline
    assert await drafts.get_draft("d1") == before


async def _assert_retry_after_injection_succeeds(db: Database) -> None:
    """注入失败不留下被占用的锁或开放事务：同一连接上重新确认成功。"""
    result = await ConfirmService(db).confirm_profile_draft(
        draft_id="d1", seen_revision=1
    )
    assert result.committed_business_version == 1
    assert (await ProfileRepo(db).read()).context_version == 1


async def _assert_injected_failure_rolls_back(
    path: Path, step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在 ``step`` 指定的那一步写入之后注入失败：断言整体回滚、锁未泄漏。

    注入点之前的步骤照常执行（确实写入了数据），回滚必须把它们一并撤销；每个步骤用独立
    临时库，步骤之间互不影响。
    """
    async with open_database(path) as db:
        drafts, baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        before = await _pending_view(drafts)
        original_profile_write = ProfileService.write_profile_in_transaction
        original_version_bump = ProfileRepo.bump_context_version_in_transaction
        original_commit_write = DraftRepo.record_commit_in_transaction

        async def failing_profile_write(self, conn, profile):
            await original_profile_write(self, conn, profile)
            raise RuntimeError("注入：档案写入之后失败")

        async def failing_version_bump(self, conn):
            await original_version_bump(self, conn)
            raise RuntimeError("注入：版本更新之后失败")

        async def failing_commit_write(self, conn, **kwargs):
            await original_commit_write(self, conn, **kwargs)
            raise RuntimeError("注入：草稿提交凭据写入之后失败（COMMIT 之前）")

        if step == "profile":
            monkeypatch.setattr(
                ProfileService, "write_profile_in_transaction", failing_profile_write
            )
        elif step == "version":
            monkeypatch.setattr(
                ProfileRepo,
                "bump_context_version_in_transaction",
                failing_version_bump,
            )
        else:
            monkeypatch.setattr(
                DraftRepo, "record_commit_in_transaction", failing_commit_write
            )

        with pytest.raises(RuntimeError, match="注入"):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d1", seen_revision=1
            )
        monkeypatch.undo()

        await _assert_everything_rolled_back(
            db, drafts, before=before, baseline=baseline
        )
        # 无锁泄漏、无开放事务：同一连接上重试成功
        await _assert_retry_after_injection_succeeds(db)


async def test_injected_failure_before_commit_rolls_back_the_whole_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分别在档案写入后、版本更新后、草稿／凭据写入后注入失败：全部回滚，无半状态。"""
    for step in ("profile", "version", "draft"):
        await _assert_injected_failure_rolls_back(
            tmp_path / f"{step}.db", step, monkeypatch
        )


async def test_cancelled_confirmation_rolls_back_and_leaves_no_lock_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交前取消：档案、版本与草稿提交状态整体回滚，取消继续传播，锁已释放。"""
    async with open_database(tmp_path / "app.db") as db:
        drafts, baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )
        before = await _pending_view(drafts)
        original_version_bump = ProfileRepo.bump_context_version_in_transaction

        async def cancelled_version_bump(self, conn):
            await original_version_bump(self, conn)
            raise asyncio.CancelledError  # 注入：版本已更新后任务被取消

        monkeypatch.setattr(
            ProfileRepo, "bump_context_version_in_transaction", cancelled_version_bump
        )
        with pytest.raises(asyncio.CancelledError):
            await ConfirmService(db).confirm_profile_draft(
                draft_id="d1", seen_revision=1
            )
        monkeypatch.undo()

        await _assert_everything_rolled_back(
            db, drafts, before=before, baseline=baseline
        )
        await _assert_retry_after_injection_succeeds(db)


# ---------- repo 接缝：版本推进只复用外层事务 ----------


async def test_version_bump_requires_an_outer_transaction(tmp_path: Path) -> None:
    """唯一版本推进语句只复用外层事务连接：不在事务内即拒绝，也不留下写入。"""
    async with open_database(tmp_path / "app.db") as db:
        _, baseline = await _setup_draft(
            db, formal=None, proposed=_first_time_profile()
        )

        async def op(conn):
            with pytest.raises(RuntimeError, match="外层事务"):
                await ProfileRepo(db).bump_context_version_in_transaction(conn)

        await db.under_lock(op)
        assert await ProfileRepo(db).read() == baseline
