"""Stage 3 行解码防线：存储类不符合列契约时抛 ``Invalid*Row``，不静默强转（P2 回归）。

SQLite 的列类型只是**亲和**而非强类型：INTEGER 亲和列照样存得下 REAL ``3.5`` 与 TEXT
``'abc'``，REAL 亲和列存得下 ``Infinity``；库层 CHECK 只管取值范围（``3.5 >= 1`` 成立）。旧解码
用裸 ``int()``／``float()``／``bool()``：``int(3.5) → 3`` 会把损坏行读成一条没人申报过的合法
事实（PR 读数凭空变小），``bool('no') → True`` 会把损坏位读成「已申报完成」。

本模块用原始 SQL 造出这些**阶段外损坏态替身**（生产写入仍只经 S3-11 确认链路），验证三个仓储
的解码一律显式失败：``domain/records/repo.py``、``domain/plan/repo.py``、``domain/stats/repo.py``。

边界：只覆盖本次被审计标记的数值／布尔解码点；不搬动既有行结构、不改写入路径、不新增过滤
口径。``completion_declared`` 一类 0／1 位在 009 有 ``CHECK`` 兜底，只有临时关掉该检查才造得出
替身（这也说明严格解码是防线而非唯一防线）。
"""

from pathlib import Path

import pytest

from domain.plan.repo import PlanRepo
from domain.plan.schema import InvalidPlanRow
from domain.records.repo import RecordRepo
from domain.records.schema import InvalidRecordRow, RecordDraftPayload
from domain.stats.schema import InvalidReviewRow
from domain.stats.service import StatsService
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage3_arrangement_confirm import _profile_and_plan, _push_session
from tests.test_stage3_record_drafts import (
    CONVERSATION_ID,
    _weight_log,
    _weight_set,
)
from tests.test_stage3_stats import (
    BARBELL_TOTAL,
    PUSH_ON,
    SQUAT_EXERCISE_ID,
    _accept,
    _confirmed,
)


async def _confirmed_session(db: Database, *, draft_id: str) -> str:
    """经真实确认链路落一条正式记录（外加负重 120kg×4 一组），返回训练身份 id。"""
    await RunRepo(db).create_conversation(CONVERSATION_ID)
    result = await _confirmed(
        db,
        draft_id=draft_id,
        occurred_on=PUSH_ON,
        exercises=(_weight_log(sets=(_weight_set(value="120", reps=4, rir=2.0),)),),
    )
    return result.training_session_id


async def _current_payload(db: Database, session_id: str) -> RecordDraftPayload | None:
    async with db.transaction() as conn:
        return await RecordRepo(db).read_current_payload_in_transaction(
            conn, session_id
        )


async def _current_payload_must_fail(db: Database, session_id: str) -> None:
    with pytest.raises(InvalidRecordRow):
        await _current_payload(db, session_id)


async def test_records_repo_rejects_fractional_counts_and_non_finite_rir(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        session_id = await _confirmed_session(db, draft_id="record-guard-counts")
        # 未损坏时可读回：证明后面的失败来自存储类，而不是库／确认链路本身坏了
        payload = await _current_payload(db, session_id)
        assert payload is not None
        assert payload.exercises[0].sets[0].reps == 4

        # 次数写成 REAL 3.5：CHECK (reps >= 1) 放行，旧解码 int(3.5) 会静默截断成 3
        async with db.transaction() as conn:
            await conn.execute("UPDATE training_sets SET reps = 3.5")
        await _current_payload_must_fail(db, session_id)

        # RIR 写成 Infinity：CHECK (rir >= 0) 放行，旧解码 float() 会原样读成 inf
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE training_sets SET reps = 4, rir = ?", (float("inf"),)
            )
        await _current_payload_must_fail(db, session_id)


async def test_records_repo_rejects_fractional_revision_no_and_invalid_boolean(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        session_id = await _confirmed_session(db, draft_id="record-guard-revision")
        repo = RecordRepo(db)

        # 修订号写成 REAL 2.5：CHECK (revision_no >= 1) 放行，旧解码 int(2.5) 会截断成 2
        async with db.transaction() as conn:
            await conn.execute("UPDATE session_revisions SET revision_no = 2.5")
        with pytest.raises(InvalidRecordRow):
            await repo.read_session(session_id)
        async with db.transaction() as conn:
            await conn.execute("UPDATE session_revisions SET revision_no = 1")

        # 0／1 位：'no'／2／0.5 都不是合法存储，旧解码 bool() 会把它们统统读成「已申报完成」
        for invalid in ("no", 2, 0.5):
            async with db.transaction() as conn:
                await conn.execute("PRAGMA ignore_check_constraints = ON")
                await conn.execute(
                    "UPDATE session_revisions SET completion_declared = ?",
                    (invalid,),
                )
                await conn.execute("PRAGMA ignore_check_constraints = OFF")
            await _current_payload_must_fail(db, session_id)


async def test_plan_repo_rejects_fractional_version_and_revision_no(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        arrangement_revision_id = await _accept(
            db, draft_id="arrangement-guard", session_id=session.id
        )
        repo = PlanRepo(db)
        arrangement = await repo.read_arrangement_revision(arrangement_revision_id)
        assert arrangement is not None
        assert arrangement.revision_no == 1

        # 安排修订号写成 REAL 2.5：旧解码 int(2.5) 会截断成 2
        async with db.transaction() as conn:
            await conn.execute("UPDATE arrangement_revisions SET revision_no = 2.5")
        with pytest.raises(InvalidPlanRow):
            await repo.read_arrangement_revision(arrangement_revision_id)
        async with db.transaction() as conn:
            await conn.execute("UPDATE arrangement_revisions SET revision_no = 1")

        # 计划版本号写成 REAL 2.5：旧解码 int(2.5) 会截断成 2，读者以为这是第 2 版
        async with db.transaction() as conn:
            await conn.execute("UPDATE plan_versions SET version = 2.5")
        with pytest.raises(InvalidPlanRow):
            await repo.read_current()


async def test_stats_repo_rejects_fractional_and_text_pr_values(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _confirmed_session(db, draft_id="record-guard-pr")
        stats = StatsService(db)
        key = {"exercise_id": SQUAT_EXERCISE_ID, "load_notation": BARBELL_TOTAL}
        assert await stats.pr_max_load(**key) == 120000
        assert await stats.pr_max_reps_at_load(**key, load_kg_key=120000) == 4

        # 换算键是 INTEGER 亲和派生列，却拦不住 REAL 120000.5：旧解码 int() 静默截断成 120000
        async with db.transaction() as conn:
            await conn.execute("UPDATE training_sets SET load_kg_key = 120000.5")
        with pytest.raises(InvalidReviewRow):
            await stats.pr_max_load(**key)

        # 次数列同理：REAL 3.5 会被截断成 3，PR 读数凭空变小
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE training_sets SET load_kg_key = 120000, reps = 3.5"
            )
        with pytest.raises(InvalidReviewRow):
            await stats.pr_max_reps_at_load(**key, load_kg_key=120000)

        # TEXT 同属损坏：'not-a-number' 过得了视图过滤（只要求非空），MAX 会直接返回它
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE training_sets SET load_kg_key = 'not-a-number', reps = 4"
            )
        with pytest.raises(InvalidReviewRow):
            await stats.pr_max_load(**key)
