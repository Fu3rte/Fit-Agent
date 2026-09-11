"""Stage 3 S3-11：训练记录确认、更正与作废（``training_record`` 草稿的确认事务）。

验收对照（stage3.md §5 S3-11、§4.2；05 5.1–5.5 与 05 验收 1–5）：

- 确认写入**完整**修订（修订行 + 全部动作事实 + 逐组事实）并原子切换 ``current_revision_id``；
  旧修订保留、旧修订不再是当前事实（05 5.3）。
- 归属显式、绝不推断：``training_session_id=None`` 新建稳定身份（同日多练各自身份），给出 id
  则向同一身份追加修订——补充同次不增加训练次数（05 5.1／5.4）。
- 更正追加到同一身份：旧修订可追溯、身份与次数不增加（05 验收 1–2）。
- 作废追加 ``voided`` 修订并成为当前修订：不物理删除、不回退旧有效版本（05 5.3、验收 3）。
- 辅助是单组信息：显式声明的 ``assistance='none'`` 落成 ``none``，未明确的保持 NULL
  （不替确认卡归类，05 5.5）；组级负重只在 ``record_type='reps_weight'`` 上成立。
- 记录绑定**执行时所依据**的安排修订，不随后续接受推进到「最新」（05 5.1）。
- 幂等凭据：重复确认返回同一结果、不重复追加修订、不重复推进 ``context_version``。

边界：全部经内部应用层（``RecordDraftService``／``ConfirmService``）与 ``tmp_path`` 临时文件
库，不接 HTTP、不触碰真实用户数据目录。用例里的原始 SQL 只用于**读取**校验，不写正式事实。
"""

import ast
import re
from pathlib import Path

import pytest

from app.confirm import (
    ConfirmService,
    DraftDiscarded,
    DraftRevisionConflict,
    DraftStale,
    RecordCommitResult,
)
from app.draft_repo import DraftRepo
from app.drafts import DraftKindMismatch, DraftService, UnknownDraft
from app.record_drafts import RecordDraftService
from domain.plan.repo import PlanRepo
from domain.plan.rules import InvalidArrangementTarget
from domain.records.rules import InvalidRecordFact
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import BACKEND_ROOT, open_database
from tests.test_stage3_arrangement_confirm import (
    _confirm_arrangement,
    _create_arrangement,
    _profile_and_plan,
    _push_session,
)
from tests.test_stage3_plan_drafts import _profile
from tests.test_stage3_record_drafts import (
    CONVERSATION_ID,
    OCCURRED_ON,
    _bodyweight_log,
    _create,
    _weight_log,
    _weight_set,
)

BENCH_ITEM_KEY = "push-01"
BENCH_EXERCISE_ID = "barbell-bench-press"

_COUNT_SQL = {
    "training_sessions": "SELECT COUNT(*) AS n FROM training_sessions",
    "session_revisions": "SELECT COUNT(*) AS n FROM session_revisions",
    "exercise_logs": "SELECT COUNT(*) AS n FROM exercise_logs",
    "training_sets": "SELECT COUNT(*) AS n FROM training_sets",
}


def _entry(view, field):
    for item in view.diff:
        if item.field == field:
            return item
    raise AssertionError(f"Diff 缺少字段：{field}")


# ---------- 入口与原始读取 ----------


async def _conversation(db: Database) -> None:
    await RunRepo(db).create_conversation(CONVERSATION_ID)


async def _confirm(
    db: Database, *, draft_id: str, seen_revision: int = 1
) -> RecordCommitResult:
    return await ConfirmService(db).confirm_record_draft(
        draft_id=draft_id, seen_revision=seen_revision
    )


async def _void(
    db: Database, *, draft_id: str, seen_revision: int = 1
) -> RecordCommitResult:
    return await ConfirmService(db).void_record_draft(
        draft_id=draft_id, seen_revision=seen_revision
    )


async def _fetch(db: Database, sql: str, params: tuple[object, ...] = ()) -> list[dict]:
    """只读原始行（SQL 一律字面量写在测试用例里，表名不参与拼接）。"""

    async def op(conn):
        async with conn.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _count(db: Database, table: str) -> int:
    rows = await _fetch(db, _COUNT_SQL[table])
    return int(rows[0]["n"])


async def _context_version(db: Database) -> int:
    rows = await _fetch(db, "SELECT context_version FROM user_profile WHERE id = 1")
    assert rows
    return int(rows[0]["context_version"])


async def _sessions(db: Database) -> list[dict]:
    return await _fetch(
        db,
        "SELECT id, current_revision_id, created_at FROM training_sessions"
        " ORDER BY created_at, id",
    )


async def _revisions(db: Database, session_id: str) -> list[dict]:
    return await _fetch(
        db,
        "SELECT id, session_id, revision_no, previous_revision_id, status, occurred_on,"
        " arrangement_revision_id, source_draft_id, confirmed_at,"
        " completion_declared, is_return_phase FROM session_revisions"
        " WHERE session_id = ? ORDER BY revision_no",
        (session_id,),
    )


async def _sets(db: Database, revision_id: str) -> list[dict]:
    return await _fetch(
        db,
        "SELECT s.set_no, s.set_type, s.load_value_text, s.load_unit, s.load_kg_key,"
        " s.reps, s.duration_seconds, s.rir, s.assistance, s.assisted_reps,"
        " s.quality_text FROM training_sets s"
        " JOIN exercise_logs l ON l.id = s.exercise_log_id"
        " WHERE l.session_revision_id = ? ORDER BY l.position, s.set_no",
        (revision_id,),
    )


async def _logs(db: Database, revision_id: str) -> list[dict]:
    return await _fetch(
        db,
        "SELECT position, exercise_id, record_type, load_notation, target_item_key,"
        " warmup_summary_text FROM exercise_logs"
        " WHERE session_revision_id = ? ORDER BY position",
        (revision_id,),
    )


# ---------- 确认：完整修订、稳定身份、版本只推一次 ----------


async def test_confirm_creates_identity_and_complete_revision_with_explicit_facts(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        warmup = _weight_log(
            position=1,
            warmup_summary_text="递增至 60kg",
            sets=(
                _weight_set(
                    set_no=1,
                    value="60",
                    reps=8,
                    set_type="work",
                    assistance="none",  # 确认卡确认「无辅助」：显式落成 none
                ),
            ),
        )
        bodyweight = _bodyweight_log(position=2)
        view = await _create(
            db, exercises=(warmup, bodyweight), feedback={"pain_report": "not_reported"}
        )
        assert view.status == "valid"
        assert await _count(db, "session_revisions") == 0
        assert await _context_version(db) == 0

        result = await _confirm(db, draft_id="record-draft-1")

        assert result.revision_no == 1
        assert result.status == "valid"
        sessions = await _sessions(db)
        assert [row["id"] for row in sessions] == [result.training_session_id]
        assert sessions[0]["current_revision_id"] == result.session_revision_id
        revisions = await _revisions(db, result.training_session_id)
        assert len(revisions) == 1
        assert revisions[0]["status"] == "valid"
        assert revisions[0]["previous_revision_id"] is None
        assert revisions[0]["occurred_on"] == OCCURRED_ON.isoformat()
        assert revisions[0]["source_draft_id"] == "record-draft-1"
        assert revisions[0]["confirmed_at"]  # 真实确认时间，不是占位值
        assert revisions[0]["arrangement_revision_id"] is None
        # 完整修订：两个动作事实 + 逐组事实（热身摘要保留原文）
        logs = await _logs(db, result.session_revision_id)
        assert [row["exercise_id"] for row in logs] == [
            "barbell-back-squat",
            "pull-up",
        ]
        assert logs[0]["warmup_summary_text"] == "递增至 60kg"
        sets = await _sets(db, result.session_revision_id)
        assert [
            (row["set_no"], row["load_value_text"], row["load_kg_key"], row["reps"])
            for row in sets
        ] == [(1, "60", 60000, 8), (1, None, None, 10)]
        # 辅助是单组信息：显式 none 落成 none；未明确的组保持 NULL（不补造）
        assert [row["assistance"] for row in sets] == ["none", None]
        assert [row["rir"] for row in sets] == [None, None]
        # 业务版本恰好 +1；草稿 Committed 与凭据同事务
        assert await _context_version(db) == 1
        draft = await DraftRepo(db).get("record-draft-1")
        assert draft is not None and draft.status == "committed"
        assert draft.committed_revision == 1
        assert draft.committed_business_version == 1

        # 幂等：所见 revision 过期也直接返回原凭据，不重复写修订、不重复推版本
        repeated = await _confirm(db, draft_id="record-draft-1", seen_revision=7)
        assert repeated == result
        assert await _count(db, "session_revisions") == 1
        assert await _context_version(db) == 1


async def test_supplementing_the_same_session_appends_a_revision_without_a_new_identity(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        await _create(db, exercises=(_weight_log(sets=(_weight_set(value="80"),)),))
        first = await _confirm(db, draft_id="record-draft-1")
        assert await _context_version(db) == 1

        # 显式补充／更正同一身份：归属从首次确认结果取，不按日期或「最新」推断
        correction = await _create(
            db,
            draft_id="record-draft-2",
            training_session_id=first.training_session_id,
            exercises=(_weight_log(sets=(_weight_set(value="60"),)),),
        )
        assert correction.baseline is not None
        assert _entry(correction, "exercises").before == (
            _weight_log(sets=(_weight_set(value="80"),)),
        )
        second = await _confirm(db, draft_id="record-draft-2")

        assert second.training_session_id == first.training_session_id
        assert second.revision_no == 2
        # 补充同次不增加训练次数：身份恒 1 行
        assert await _count(db, "training_sessions") == 1
        revisions = await _revisions(db, first.training_session_id)
        assert [row["revision_no"] for row in revisions] == [1, 2]
        assert revisions[1]["previous_revision_id"] == first.session_revision_id
        # 旧修订可追溯：事实未被改写、也不再是当前事实
        assert [
            row["load_value_text"] for row in await _sets(db, first.session_revision_id)
        ] == ["80"]
        assert [
            row["load_value_text"]
            for row in await _sets(db, second.session_revision_id)
        ] == ["60"]
        sessions = await _sessions(db)
        assert sessions[0]["current_revision_id"] == second.session_revision_id
        assert sessions[0]["current_revision_id"] != first.session_revision_id
        assert await _context_version(db) == 2  # 一次确认恰好 +1

        repeated = await _confirm(db, draft_id="record-draft-2")
        assert repeated == second
        assert await _count(db, "session_revisions") == 2
        assert await _context_version(db) == 2


async def test_two_confirmations_on_the_same_day_are_distinct_identities(
    tmp_path: Path,
) -> None:
    """日期不是身份：同日两次确认各得独立 ``training_sessions``（05 验收 2、5.4）。"""
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        await _create(db, draft_id="record-draft-1")
        morning = await _confirm(db, draft_id="record-draft-1")
        # 第二次在第一次确认后按最新基线准备（每次确认推一次业务版本，草稿基线随之刷新）
        await _create(db, draft_id="record-draft-2", occurred_on=OCCURRED_ON)
        evening = await _confirm(db, draft_id="record-draft-2")

        assert morning.training_session_id != evening.training_session_id
        sessions = await _sessions(db)
        assert len(sessions) == 2
        assert {row["id"] for row in sessions} == {
            morning.training_session_id,
            evening.training_session_id,
        }
        assert all(row["current_revision_id"] is not None for row in sessions)
        assert [
            row["revision_no"]
            for row in await _revisions(db, morning.training_session_id)
        ] == [1]


async def test_incomplete_revision_is_confirmed_with_only_explicit_facts(
    tmp_path: Path,
) -> None:
    """D8 已拍 A：允许确认为 ``incomplete`` 修订；未明确的事实保持为空、不补造。"""
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        await _create(
            db,
            exercises=(
                _weight_log(sets=(_weight_set(set_type=None, value="50", reps=5),)),
            ),
        )
        result = await _confirm(db, draft_id="record-draft-1")

        assert result.status == "incomplete"
        sets = await _sets(db, result.session_revision_id)
        assert sets[0]["set_type"] is None  # 不是工作组
        assert sets[0]["assistance"] is None  # 不是「无辅助」
        assert sets[0]["load_value_text"] == "50"  # 已明确的事实落盘


# ---------- 作废：追加并成为当前，不删除、不回退 ----------


async def test_void_appends_voided_revision_and_never_deletes_or_reverts(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        await _create(db, exercises=(_weight_log(sets=(_weight_set(value="80"),)),))
        valid = await _confirm(db, draft_id="record-draft-1")

        await _create(
            db,
            draft_id="record-draft-2",
            training_session_id=valid.training_session_id,
            exercises=(_weight_log(sets=(_weight_set(value="80"),)),),
        )
        voided = await _void(db, draft_id="record-draft-2")

        assert voided.training_session_id == valid.training_session_id
        assert voided.revision_no == 2
        assert voided.status == "voided"
        revisions = await _revisions(db, valid.training_session_id)
        assert [row["status"] for row in revisions] == ["valid", "voided"]
        assert revisions[1]["previous_revision_id"] == valid.session_revision_id
        # 不物理删除、不回退：旧有效修订与其事实仍在，当前指针指向 voided 修订
        assert await _count(db, "training_sessions") == 1
        assert [
            row["load_value_text"] for row in await _sets(db, valid.session_revision_id)
        ] == ["80"]
        sessions = await _sessions(db)
        assert sessions[0]["current_revision_id"] == voided.session_revision_id
        assert await _context_version(db) == 2

        repeated = await _void(db, draft_id="record-draft-2")
        assert repeated == voided
        assert await _count(db, "session_revisions") == 2
        assert await _context_version(db) == 2


async def test_voiding_requires_an_existing_identity_and_other_kinds_are_rejected(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        # 尚未建立的训练不能「作废」：显式新增的草稿不写任何正式事实
        await _create(db, draft_id="record-draft-1")
        with pytest.raises(InvalidRecordFact):
            await _void(db, draft_id="record-draft-1")
        assert await _count(db, "training_sessions") == 0
        assert await _count(db, "session_revisions") == 0
        assert await _context_version(db) == 0

        # kind 分派：非记录草稿不被记录确认入口按记录形状解码
        drafts = DraftService(db)
        await drafts.create_profile_draft(
            draft_id="profile-draft-1",
            generation_baseline=await drafts.prepare_generation_baseline(),
            conversation_id=CONVERSATION_ID,
            run_id=None,
            proposed=_profile(),
        )
        with pytest.raises(DraftKindMismatch):
            await _confirm(db, draft_id="profile-draft-1")
        with pytest.raises(DraftKindMismatch):
            await _void(db, draft_id="profile-draft-1")


# ---------- 绑定实际使用的安排修订，不随后续接受改绑 ----------


async def test_record_binds_the_arrangement_revision_actually_used(
    tmp_path: Path,
) -> None:
    """05 5.1：记录关联执行时依据的安排快照；后续接受新调整不改写已确认记录的绑定。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)
        used = await _confirm_arrangement(db, draft_id="arr-draft-1")
        await _create_arrangement(
            db, draft_id="arr-draft-2", session_id=session.id, work_sets=1
        )
        later = await _confirm_arrangement(db, draft_id="arr-draft-2")
        assert later.arrangement_revision_id != used.arrangement_revision_id

        service = RecordDraftService(db)
        preparation = await service.prepare_input(session.scheduled_on)
        await service.create_record_draft(
            draft_id="record-draft-1",
            preparation=preparation,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            training_session_id=None,
            exercises=(
                _weight_log(
                    exercise_id=BENCH_EXERCISE_ID, target_item_key=BENCH_ITEM_KEY
                ),
            ),
            arrangement_revision_id=used.arrangement_revision_id,
        )
        result = await _confirm(db, draft_id="record-draft-1")

        revisions = await _revisions(db, result.training_session_id)
        assert revisions[0]["arrangement_revision_id"] == used.arrangement_revision_id
        assert revisions[0]["arrangement_revision_id"] != later.arrangement_revision_id
        # 「当刻最新」确实已被后续接受推进，证明上面落的是草案绑定的那一条
        latest = await PlanRepo(db).read_latest_arrangement(session.id)
        assert latest is not None
        assert latest.id == later.arrangement_revision_id

        # 确认时仍复查：安排修订不存在即拒结、零写入
        await _create(
            db,
            draft_id="record-draft-2",
            exercises=(_weight_log(),),
            arrangement_revision_id=used.arrangement_revision_id,
        )
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE business_drafts SET proposed_record_json = ? WHERE id = ?",
                (
                    '{"schema_version": 1, "occurred_on": "'
                    + OCCURRED_ON.isoformat()
                    + '", "arrangement_revision_id": "missing", "exercises": []}',
                    "record-draft-2",
                ),
            )
        with pytest.raises(InvalidArrangementTarget):
            await _confirm(db, draft_id="record-draft-2")
        assert await _count(db, "session_revisions") == 1


# ---------- 拦截：过期、旧 revision、已丢弃、未知草稿零写入 ----------


async def test_guards_reject_stale_revision_discarded_and_unknown_with_zero_writes(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        await _create(db, draft_id="record-draft-1")
        await _create(db, draft_id="record-draft-2")
        await _create(db, draft_id="record-draft-3")
        await _create(db, draft_id="record-draft-4")

        # 原 revision 不符：拦在写入之前
        with pytest.raises(DraftRevisionConflict):
            await _confirm(db, draft_id="record-draft-1", seen_revision=7)
        # 基线过期：另一草稿已推版本（真实确认），旧草稿保持零写入
        await _confirm(db, draft_id="record-draft-2")
        with pytest.raises(DraftStale):
            await _confirm(db, draft_id="record-draft-1")
        # 已丢弃（记录草稿的丢弃入口未在 S3-11 工作项内，用共享 repo 条件更新做替身）
        async with db.transaction() as conn:
            await DraftRepo(db).record_discard_in_transaction(
                conn, draft_id="record-draft-3"
            )
        with pytest.raises(DraftDiscarded):
            await _confirm(db, draft_id="record-draft-3")
        # 未知草稿不新建
        with pytest.raises(UnknownDraft):
            await _confirm(db, draft_id="nope")

        assert await _count(db, "training_sessions") == 1
        assert await _count(db, "session_revisions") == 1
        assert await _context_version(db) == 1
        discarded = await DraftRepo(db).get("record-draft-3")
        assert discarded is not None and discarded.status == "discarded"


# ---------- 追加不删除：写入路径只有 INSERT 与当前指针切换 ----------


_APPEND_ONLY_WRITE_SQL = re.compile(
    r"(?is)\b(insert\s+(or\s+\w+\s+)?into|update|delete\s+from|replace\s+into)\s+(\w+)"
)
_RECORD_TABLES = {
    "training_sessions",
    "session_revisions",
    "exercise_logs",
    "training_sets",
}


def test_record_repo_has_only_append_only_write_paths() -> None:
    """S3-11 关掉 S3-09 证据残留③：库层不可变性由「明确写入路径」满足。

    记录侧正式事实的唯一写入入口 ``domain/records/repo.py`` 只允许 INSERT 修订／动作／组事实，
    唯一 UPDATE 是 ``training_sessions.current_revision_id``（旧修订与子行结构上改不到、删不掉）。
    没有触发器是因为合同允许「明确写入路径＋必要触发器」二选一（报告 :141）。
    """
    source = (BACKEND_ROOT / "domain" / "records" / "repo.py").read_text(
        encoding="utf-8"
    )
    literals = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    # 只看以写入关键字开头的字面量（写法与 repo 一致：语句首行单独成串），
    # 不把注释／docstring 里的叙述词（如「唯一 UPDATE 是…」）当成语句。
    statements = [
        literal
        for literal in literals
        if literal.lstrip()
        .upper()
        .startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE "))
    ]
    writes = [
        match
        for literal in statements
        for match in _APPEND_ONLY_WRITE_SQL.findall(literal)
    ]
    assert writes, "守卫失效：没扫到任何写入语句"
    assert {match[2] for match in writes} <= _RECORD_TABLES
    updates = [match for match in writes if match[0].lower().startswith("update")]
    assert updates == [("UPDATE", "", "training_sessions")], updates
    assert not [match for match in writes if "delete" in match[0].lower()]
