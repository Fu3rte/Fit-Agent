"""Stage 3 S3-10：训练记录草稿创建、查询与结构化 Diff、纠错（``training_record``）。

验收对照（stage3.md §5 S3-10、§4.1；05 5.1–5.5、01 1.3、§5 S3-05 同口径）：

- 内部准备打卡结构化草稿（动作、组次、热身摘要、安排关联可空）；查询返回后端按快照现算的
  结构化 Diff。
- 归属与安排关联**显式**：``training_session_id`` 无默认值（``None`` = 显式新增一次训练，
  给出 id = 补充／更正既有身份）；同日多练时日期不唯一，准备结果列出该日既有身份供显式
  选择，不按日期推断；``arrangement_revision_id`` 可空且从不推断。
- 允许确认为 ``incomplete`` 修订（已明确事实落盘）；补全后经纠错变 ``valid``（状态按事实
  完整性派生，不存第二份状态）；未明确的 RIR／质量保持空。
- 纠错白名单与乐观并发：归属与安排关联不可改；旧 revision 拒绝；终态不可纠错；非法纠错
  零写入。
- 只拟议不落正式事实：创建、查询与纠错都不写记录侧四表、不改档案、不推进 ``context_version``；
  重开后内容一致。

边界：全部经内部应用层（``RecordDraftService``）与 ``tmp_path`` 临时文件库，不接 HTTP、不
触碰真实用户数据目录。用例里的原始 SQL 只用于造出「既有训练身份／修订」这一阶段外替身
（记录侧正式写入——确认追加修订与切换当前指针——归 S3-11），不是生产写入旁路。
"""

import inspect
from datetime import date
from pathlib import Path

import pytest

from app.draft_repo import DraftRepo
from app.drafts import (
    DraftKindMismatch,
    DraftNotCorrectable,
    DraftRevisionConflict,
    DraftService,
    UnknownDraft,
)
from app.record_drafts import (
    RECORD_DRAFT_KIND,
    RecordDraftService,
    RecordDraftView,
)
from domain.plan.rules import InvalidArrangementTarget
from domain.profile.rules import UnknownExerciseReference
from domain.records.rules import (
    InvalidRecordFact,
    load_kg_key,
    record_draft_status,
    validate_record_draft_correction,
)
from domain.records.schema import (
    DraftExerciseLog,
    ExerciseLogFacts,
    RawLoad,
    RecordDraftPayload,
    SetFacts,
    record_draft_from_json,
    record_draft_to_json,
)
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage3_arrangement_confirm import (
    _confirm_arrangement,
    _create_arrangement,
    _profile_and_plan,
    _push_session,
)
from tests.test_stage3_plan_drafts import _profile

CONVERSATION_ID = "c1"
OCCURRED_ON = date(2026, 9, 16)
BENCH_ITEM_KEY = "push-01"  # PPL push 日第 1 项：平板杠铃卧推（item_key 同 S3-08 用例）
BENCH_EXERCISE_ID = "barbell-bench-press"


# ---------- 载荷构件 ----------


def _weight_set(
    set_no: int = 1,
    *,
    set_type: str | None = "work",
    value: str = "60",
    unit: str = "kg",
    reps: int | None = 8,
    rir: float | None = None,
    assistance: str | None = None,
    quality_text: str | None = None,
    assisted_reps: int | None = None,
    duration_seconds: int | None = None,
    load: bool = True,
) -> SetFacts:
    """外加负重次数型的一组：默认完整工作组；未明确的事实默认保持空。"""
    return SetFacts(
        set_no=set_no,
        set_type=set_type,  # type: ignore[arg-type]
        load=RawLoad(value, unit) if load else None,  # type: ignore[arg-type]
        reps=reps,
        duration_seconds=duration_seconds,
        rir=rir,
        assistance=assistance,  # type: ignore[arg-type]
        assisted_reps=assisted_reps,
        quality_text=quality_text,
    )


def _weight_log(
    position: int = 1,
    *,
    sets: tuple[SetFacts, ...] = (),
    exercise_id: str = "barbell-back-squat",
    load_notation: str | None = "barbell_includes_bar_total",
    target_item_key: str | None = None,
    warmup_summary_text: str | None = None,
) -> DraftExerciseLog:
    """外加负重次数型动作事实；默认带一组完整工作组。"""
    if not sets:
        sets = (_weight_set(),)
    return DraftExerciseLog(
        position=position,
        facts=ExerciseLogFacts(
            exercise_id=exercise_id,
            record_type="reps_weight",
            load_notation=load_notation,  # type: ignore[arg-type]
            target_item_key=target_item_key,
            warmup_summary_text=warmup_summary_text,
        ),
        sets=sets,
    )


def _bodyweight_log(
    position: int = 1, *, sets: tuple[SetFacts, ...] = ()
) -> DraftExerciseLog:
    """自重次数型动作事实（不虚构 0kg、不带负重口径）。"""
    if not sets:
        sets = (SetFacts(set_no=1, set_type="work", reps=10),)
    return DraftExerciseLog(
        position=position,
        facts=ExerciseLogFacts(exercise_id="pull-up", record_type="reps_bodyweight"),
        sets=sets,
    )


# ---------- 入口与行读取 ----------


async def _conversation(db: Database) -> None:
    await RunRepo(db).create_conversation(CONVERSATION_ID)


async def _create(
    db: Database,
    *,
    draft_id: str = "record-draft-1",
    occurred_on: date = OCCURRED_ON,
    training_session_id: str | None = None,
    exercises: tuple[DraftExerciseLog, ...] = (),
    arrangement_revision_id: str | None = None,
    **overrides: object,
) -> RecordDraftView:
    """经真实内部准备入口创建一条 Pending 记录草稿。"""
    if not exercises:
        exercises = (_weight_log(),)
    service = RecordDraftService(db)
    preparation = await service.prepare_input(occurred_on)
    return await service.create_record_draft(
        draft_id=draft_id,
        preparation=preparation,
        conversation_id=CONVERSATION_ID,
        run_id=None,
        training_session_id=training_session_id,
        exercises=exercises,
        arrangement_revision_id=arrangement_revision_id,
        **overrides,  # type: ignore[arg-type]
    )


async def _draft_row(db: Database, draft_id: str) -> dict[str, object]:
    """``business_drafts`` 原始行（列级不可变断言用）。"""

    async def op(conn):
        async with conn.execute(
            "SELECT * FROM business_drafts WHERE id = ?", (draft_id,)
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    return await db.under_lock(op)


async def _draft_ids(db: Database) -> list[str]:
    async def op(conn):
        async with conn.execute("SELECT id FROM business_drafts ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        return [str(row["id"]) for row in rows]

    return await db.under_lock(op)


async def _formal_counts(db: Database) -> tuple[int, int, int, int]:
    """记录侧四表行数：正式事实只经 S3-11 确认事务写入，本任务恒为 0。"""

    async def op(conn):
        counts = []
        for table in (
            "training_sessions",
            "session_revisions",
            "exercise_logs",
            "training_sets",
        ):
            counts.append(await _count(conn, table))
        return tuple(counts)

    return await db.under_lock(op)  # type: ignore[return-value]


async def _context_version(db: Database) -> int:
    async def op(conn):
        async with conn.execute(
            "SELECT context_version FROM user_profile WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row["context_version"])

    return await db.under_lock(op)


# 固定白名单：测试只按表名常量查行数，不拼接任意表名。
_COUNT_SQL = {
    "training_sessions": "SELECT COUNT(*) FROM training_sessions",
    "session_revisions": "SELECT COUNT(*) FROM session_revisions",
    "exercise_logs": "SELECT COUNT(*) FROM exercise_logs",
    "training_sets": "SELECT COUNT(*) FROM training_sets",
    "arrangement_revisions": "SELECT COUNT(*) FROM arrangement_revisions",
    "plan_versions": "SELECT COUNT(*) FROM plan_versions",
}


async def _count(conn, table: str) -> int:
    async with conn.execute(_COUNT_SQL[table]) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _raw_count(db: Database, table: str) -> int:
    return await db.under_lock(lambda conn: _count(conn, table))


async def _seed_confirmed_session(
    db: Database,
    *,
    session_id: str,
    revision_id: str,
    occurred_on: date,
    exercises: tuple[DraftExerciseLog, ...] = (),
    status: str = "valid",
) -> None:
    """阶段外替身：造出既有训练身份与当前修订（05 5.1/5.3 的行形状）。

    记录侧正式写入（确认追加修订、切换当前修订指针）归 S3-11；本替身只用于「同日多练归属
    歧义」与「更正草稿 Diff 基线」两类用例。``source_draft_id`` 指向一条真实的来源草稿行，
    手动重跑历史用例时可整体替换为真实确认链路。
    """
    stamp = "2026-09-16T10:00:00+00:00"
    source_draft_id = f"draft-{session_id}"
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO business_drafts (id, kind, conversation_id, run_id,"
            " base_profile_json, proposed_profile_json, base_business_version,"
            " revision, status, created_at, updated_at)"
            " VALUES (?, 'training_record', ?, NULL, NULL, '{}', 0, 1,"
            " 'pending', ?, ?)",
            (source_draft_id, CONVERSATION_ID, stamp, stamp),
        )
        await conn.execute(
            "INSERT INTO training_sessions (id, created_at) VALUES (?, ?)",
            (session_id, stamp),
        )
        await conn.execute(
            "INSERT INTO session_revisions (id, session_id, revision_no, status,"
            " occurred_on, source_draft_id, confirmed_at)"
            " VALUES (?, ?, 1, ?, ?, ?, ?)",
            (
                revision_id,
                session_id,
                status,
                occurred_on.isoformat(),
                source_draft_id,
                stamp,
            ),
        )
        for exercise in exercises:
            log_id = f"{revision_id}-log-{exercise.position}"
            facts = exercise.facts
            await conn.execute(
                "INSERT INTO exercise_logs (id, session_revision_id, exercise_id,"
                " position, target_item_key, record_type, load_notation,"
                " warmup_summary_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    log_id,
                    revision_id,
                    facts.exercise_id,
                    exercise.position,
                    facts.target_item_key,
                    facts.record_type,
                    facts.load_notation,
                    facts.warmup_summary_text,
                ),
            )
            for single in exercise.sets:
                await conn.execute(
                    "INSERT INTO training_sets (id, exercise_log_id, set_no,"
                    " set_type, target_set_key, load_value_text, load_unit,"
                    " load_kg_key, reps, duration_seconds, rir, assistance,"
                    " assisted_reps, quality_text)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{log_id}-set-{single.set_no}",
                        log_id,
                        single.set_no,
                        single.set_type,
                        single.target_set_key,
                        None if single.load is None else single.load.value_text,
                        None if single.load is None else single.load.unit,
                        load_kg_key(single.load),
                        single.reps,
                        single.duration_seconds,
                        single.rir,
                        single.assistance,
                        single.assisted_reps,
                        single.quality_text,
                    ),
                )
        await conn.execute(
            "UPDATE training_sessions SET current_revision_id = ? WHERE id = ?",
            (revision_id, session_id),
        )


def _entry(view: RecordDraftView, field: str):
    for item in view.diff:
        if item.field == field:
            return item
    raise AssertionError(f"Diff 缺少字段：{field}")


# ---------- 创建：只拟议，不落正式事实 ----------


async def test_creating_a_draft_writes_only_the_draft_and_keeps_unknowns_empty(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        before_profile = await _context_version(db)

        view = await _create(db)

        assert view.draft.kind == RECORD_DRAFT_KIND
        assert view.draft.status == "pending"
        assert view.draft.revision == 1
        assert view.draft.proposed_record_json is not None
        assert view.payload.occurred_on == OCCURRED_ON
        assert view.payload.training_session_id is None  # 显式新增一次训练
        assert view.payload.arrangement_revision_id is None  # 无安排不强行套用
        assert view.status == "valid"
        single = view.payload.exercises[0].sets[0]
        # 未明确的 RIR／质量／辅助保持空：不被读作 RIR 0、无辅助或工作组之外的默认值
        assert single.rir is None
        assert single.quality_text is None
        assert single.assistance is None
        assert single.assisted_reps is None
        # 只拟议：记录侧四表零写入、业务版本不推进、档案不动
        assert await _formal_counts(db) == (0, 0, 0, 0)
        assert await _context_version(db) == before_profile == 0
        # 落库载荷与查询形态一致（未明确事实在存储层同样为空，不补造）
        stored = record_draft_from_json(str(view.draft.proposed_record_json))
        assert stored == view.payload
        assert stored.exercises[0].sets[0].rir is None
        # 查询两个入口一致
        again = await RecordDraftService(db).get_record_draft("record-draft-1")
        assert again is not None
        assert again.payload == view.payload
        assert again.status == view.status
        listed = await RecordDraftService(db).list_record_drafts(CONVERSATION_ID)
        assert [item.draft.id for item in listed] == ["record-draft-1"]


async def test_incomplete_draft_is_saved_with_explicit_facts_and_lists_by_kind(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        # 组类型未明确（用户还没确认热身／工作组）：允许保存待补全载荷（D8 已拍 A）
        incomplete = _weight_log(sets=(_weight_set(set_type=None, rir=1.5),))
        view = await _create(db, exercises=(incomplete,), draft_id="record-draft-1")
        assert view.status == "incomplete"
        assert view.payload.exercises[0].sets[0].rir == 1.5  # 已明确的事实落盘

        # 档案草稿与记录草稿同会话共存：各自查询入口按 kind 过滤，不互相解码
        drafts = DraftService(db)
        baseline = await drafts.prepare_generation_baseline()
        await drafts.create_profile_draft(
            draft_id="profile-draft-1",
            generation_baseline=baseline,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            proposed=_profile(),
        )
        listed = await RecordDraftService(db).list_record_drafts(CONVERSATION_ID)
        assert [item.draft.id for item in listed] == ["record-draft-1"]
        assert [
            item.draft.id for item in await drafts.list_drafts(CONVERSATION_ID)
        ] == ["profile-draft-1"]


async def test_record_entry_rejects_other_draft_kinds(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        drafts = DraftService(db)
        baseline = await drafts.prepare_generation_baseline()
        await drafts.create_profile_draft(
            draft_id="profile-draft-1",
            generation_baseline=baseline,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            proposed=_profile(),
        )
        service = RecordDraftService(db)
        with pytest.raises(DraftKindMismatch):
            await service.get_record_draft("profile-draft-1")
        with pytest.raises(DraftKindMismatch):
            await service.revise_record_draft(
                draft_id="profile-draft-1",
                seen_revision=1,
                payload=RecordDraftPayload(
                    occurred_on=OCCURRED_ON,
                    training_session_id=None,
                    exercises=(_weight_log(),),
                ),
            )
        # 记录草稿也不会被档案草稿入口按档案形状解码
        await _create(db)
        with pytest.raises(DraftKindMismatch):
            await DraftService(db).get_draft("record-draft-1")


# ---------- 同日多练：归属显式，不按日期推断 ----------


def test_training_session_target_has_no_default() -> None:
    """归属是必填关键字参数：没有默认值就没有「没想过」的静默路径（05 5.2 目标不唯一必须询问）。"""
    parameter = inspect.signature(RecordDraftService.create_record_draft).parameters[
        "training_session_id"
    ]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


async def test_same_day_sessions_are_listed_and_amendment_targets_explicitly(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        baseline_exercise = _weight_log(sets=(_weight_set(value="70", reps=6, rir=2),))
        await _seed_confirmed_session(
            db,
            session_id="s1",
            revision_id="r1",
            occurred_on=OCCURRED_ON,
            exercises=(baseline_exercise,),
        )
        await _seed_confirmed_session(
            db,
            session_id="s2",
            revision_id="r2",
            occurred_on=OCCURRED_ON,
        )
        await _seed_confirmed_session(
            db,
            session_id="s3",
            revision_id="r3",
            occurred_on=date(2026, 9, 17),
        )

        service = RecordDraftService(db)
        preparation = await service.prepare_input(OCCURRED_ON)
        # 同日多练的归属歧义被显式列出（另一天的不列出，不按「最新」等隐含规则挑选）
        assert [item.id for item in preparation.same_day_sessions] == ["s1", "s2"]

        # 显式更正既有身份：归属写入载荷、基线给出、Diff 是「基线 → 拟议」
        view = await service.create_record_draft(
            draft_id="record-draft-1",
            preparation=preparation,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            training_session_id="s1",
            exercises=(_weight_log(sets=(_weight_set(value="65", reps=6, rir=2),)),),
        )
        assert view.payload.training_session_id == "s1"
        assert view.baseline is not None
        assert view.baseline.training_session_id == "s1"
        assert _entry(view, "exercises").before == (baseline_exercise,)
        assert _entry(view, "exercises").after == view.payload.exercises
        assert _entry(view, "exercises").changed
        assert not _entry(view, "occurred_on").changed

        # 显式新增一次训练（同日已有两次）：None 不被解析成任何既有身份
        fresh = await service.create_record_draft(
            draft_id="record-draft-2",
            preparation=preparation,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            training_session_id=None,
            exercises=(_weight_log(),),
        )
        assert fresh.payload.training_session_id is None
        assert fresh.baseline is None
        assert all(item.before is None for item in fresh.diff)
        # 查询草稿不等于写正式事实：替身以外的身份零新增
        assert await _raw_count(db, "training_sessions") == 3

        # 未知身份不猜、不建：显式给出必须真实存在
        with pytest.raises(UnknownDraft):
            await service.create_record_draft(
                draft_id="record-draft-3",
                preparation=preparation,
                conversation_id=CONVERSATION_ID,
                run_id=None,
                training_session_id="nope",
                exercises=(_weight_log(),),
            )
        assert await _draft_ids(db) == [
            "draft-s1",
            "draft-s2",
            "draft-s3",
            "record-draft-1",
            "record-draft-2",
        ]


# ---------- 安排关联：可空、显式、准确匹配 ----------


async def test_arrangement_link_is_explicit_and_never_inferred(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)
        accepted = await _confirm_arrangement(db, draft_id="arr-draft-1")
        link_id = accepted.arrangement_revision_id

        service = RecordDraftService(db)
        preparation = await service.prepare_input(session.scheduled_on)
        # 该日已有被接受的安排，但未显式给出关联时草稿保持无关联（不按日期／计划推断）
        view = await service.create_record_draft(
            draft_id="record-draft-1",
            preparation=preparation,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            training_session_id=None,
            exercises=(_weight_log(),),
        )
        assert view.payload.arrangement_revision_id is None

        # 显式给出且目标项准确对应：接受；动作身份不符或目标项不存在：拒绝、零写入
        plan_record = await service.create_record_draft(
            draft_id="record-draft-2",
            preparation=preparation,
            conversation_id=CONVERSATION_ID,
            run_id=None,
            training_session_id=None,
            exercises=(
                _weight_log(
                    exercise_id=BENCH_EXERCISE_ID, target_item_key=BENCH_ITEM_KEY
                ),
            ),
            arrangement_revision_id=link_id,
        )
        assert plan_record.payload.arrangement_revision_id == link_id
        with pytest.raises(InvalidArrangementTarget):
            await service.create_record_draft(
                draft_id="record-draft-3",
                preparation=preparation,
                conversation_id=CONVERSATION_ID,
                run_id=None,
                training_session_id=None,
                exercises=(
                    _weight_log(
                        exercise_id=BENCH_EXERCISE_ID, target_item_key="push-09"
                    ),
                ),
                arrangement_revision_id=link_id,
            )
        with pytest.raises(InvalidArrangementTarget):
            await service.create_record_draft(
                draft_id="record-draft-4",
                preparation=preparation,
                conversation_id=CONVERSATION_ID,
                run_id=None,
                training_session_id=None,
                exercises=(
                    _weight_log(
                        exercise_id="barbell-back-squat",
                        target_item_key=BENCH_ITEM_KEY,
                    ),
                ),
                arrangement_revision_id=link_id,
            )
        with pytest.raises(InvalidArrangementTarget):
            await service.create_record_draft(
                draft_id="record-draft-5",
                preparation=preparation,
                conversation_id=CONVERSATION_ID,
                run_id=None,
                training_session_id=None,
                exercises=(_weight_log(),),
                arrangement_revision_id="missing",
            )
        assert await _draft_ids(db) == [
            "arr-draft-1",
            "plan-draft-1",
            "profile-draft-1",
            "record-draft-1",
            "record-draft-2",
        ]
        # 记录草稿只拟议：安排修订与计划／日程不被改写
        assert await _raw_count(db, "arrangement_revisions") == 1


# ---------- 结构校验：非法事实零写入 ----------


async def test_invalid_facts_are_rejected_without_a_draft_row(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        service = RecordDraftService(db)
        preparation = await service.prepare_input(OCCURRED_ON)

        async def attempt(draft_id: str, exercises: tuple[DraftExerciseLog, ...]):
            return await service.create_record_draft(
                draft_id=draft_id,
                preparation=preparation,
                conversation_id=CONVERSATION_ID,
                run_id=None,
                training_session_id=None,
                exercises=exercises,
            )

        with pytest.raises(UnknownExerciseReference):
            await attempt("bad-1", (_weight_log(exercise_id="not-in-catalog"),))
        with pytest.raises(InvalidRecordFact):
            await attempt(
                "bad-2",
                (
                    DraftExerciseLog(
                        position=1,
                        facts=ExerciseLogFacts(
                            exercise_id="pull-up",
                            record_type="cardio",  # type: ignore[arg-type]
                        ),
                        sets=(SetFacts(set_no=1, set_type="work", reps=10),),
                    ),
                ),
            )  # record_type 不在目录三类：不是记录口径
        with pytest.raises(InvalidRecordFact):
            await attempt(
                "bad-3",
                (
                    DraftExerciseLog(
                        position=1,
                        facts=ExerciseLogFacts(
                            exercise_id="pull-up",
                            record_type="reps_bodyweight",
                            load_notation="dumbbell_per_hand",
                        ),
                        sets=(SetFacts(set_no=1, set_type="work", reps=10),),
                    ),
                ),
            )  # 自重型不得带负重口径
        with pytest.raises(InvalidRecordFact):
            await attempt(
                "bad-4",
                (_weight_log(sets=(_weight_set(set_no=2),)),),  # set_no 必须从 1 起连续
            )
        with pytest.raises(InvalidRecordFact):
            await attempt("bad-5", (_weight_log(sets=(_weight_set(rir=-1.0),)),))
        with pytest.raises(InvalidRecordFact):
            await attempt(
                "bad-6", (_weight_log(sets=(_weight_set(assistance="helper"),)),)
            )
        with pytest.raises(InvalidRecordFact):
            await attempt("bad-7", (_weight_log(sets=(_weight_set(reps=0),)),))
        assert await _draft_ids(db) == []


async def test_started_at_requires_precision_and_iso_timestamp(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        service = RecordDraftService(db)
        preparation = await service.prepare_input(OCCURRED_ON)

        async def attempt(draft_id: str, **overrides: object):
            return await service.create_record_draft(
                draft_id=draft_id,
                preparation=preparation,
                conversation_id=CONVERSATION_ID,
                run_id=None,
                training_session_id=None,
                exercises=(_weight_log(),),
                **overrides,  # type: ignore[arg-type]
            )

        view = await attempt(
            "record-draft-1",
            started_at="2026-09-16T19:30:00+08:00",
            time_precision="timestamp",
        )
        assert view.payload.time_precision == "timestamp"
        with pytest.raises(InvalidRecordFact):
            await attempt("bad-1", started_at="2026-09-16T19:30:00+08:00")
        with pytest.raises(InvalidRecordFact):
            await attempt("bad-2", time_precision="timestamp")
        with pytest.raises(InvalidRecordFact):
            await attempt(
                "bad-3",
                started_at="2026-09-16T19:30:00",
                time_precision="timestamp",
            )
        with pytest.raises(InvalidRecordFact):
            await attempt("bad-4", time_precision="date")
        assert await _draft_ids(db) == ["record-draft-1"]


# ---------- 纠错：白名单、乐观并发与终态 ----------


async def test_incomplete_to_valid_correction_bumps_revision_and_changes_only_payload(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        incomplete = _weight_log(sets=(_weight_set(set_type=None),))
        view = await _create(db, exercises=(incomplete,))
        assert view.status == "incomplete"
        before = await _draft_row(db, "record-draft-1")

        corrected = await RecordDraftService(db).revise_record_draft(
            draft_id="record-draft-1",
            seen_revision=1,
            payload=RecordDraftPayload(
                occurred_on=OCCURRED_ON,
                training_session_id=None,
                exercises=(_weight_log(),),
            ),
        )

        assert corrected.draft.revision == 2
        assert corrected.status == "valid"  # 补全后经纠错变有效（D8）
        after = await _draft_row(db, "record-draft-1")
        assert {key for key in after if after[key] != before[key]} <= {
            "proposed_record_json",
            "revision",
            "updated_at",
        }
        assert await _formal_counts(db) == (0, 0, 0, 0)
        assert await _context_version(db) == 0


async def test_revision_correction_can_change_facts_but_not_ownership_or_link(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        await _seed_confirmed_session(
            db, session_id="s1", revision_id="r1", occurred_on=OCCURRED_ON
        )
        view = await _create(db, training_session_id="s1")
        service = RecordDraftService(db)

        # 允许：日期（实际发生日期纠正）、热身摘要、组事实
        corrected = await service.revise_record_draft(
            draft_id="record-draft-1",
            seen_revision=1,
            payload=RecordDraftPayload(
                occurred_on=date(2026, 9, 15),
                training_session_id="s1",
                exercises=(
                    _weight_log(
                        warmup_summary_text="递增至 60kg",
                        sets=(_weight_set(value="57.5", reps=8, rir=None),),
                    ),
                ),
            ),
        )
        assert corrected.payload.occurred_on == date(2026, 9, 15)
        assert corrected.payload.exercises[0].facts.warmup_summary_text == "递增至 60kg"
        assert _entry(corrected, "occurred_on").changed
        assert _entry(corrected, "exercises").changed

        # 不允许：借纠错改归属或安排关联（改归属必须重新提问）
        with pytest.raises(InvalidRecordFact):
            await service.revise_record_draft(
                draft_id="record-draft-1",
                seen_revision=2,
                payload=RecordDraftPayload(
                    occurred_on=corrected.payload.occurred_on,
                    training_session_id=None,
                    exercises=corrected.payload.exercises,
                ),
            )
        with pytest.raises(InvalidRecordFact):
            await service.revise_record_draft(
                draft_id="record-draft-1",
                seen_revision=2,
                payload=RecordDraftPayload(
                    occurred_on=corrected.payload.occurred_on,
                    training_session_id="s1",
                    exercises=corrected.payload.exercises,
                    arrangement_revision_id="arr-1",
                ),
            )
        # 白名单拒绝发生在任何写入之前：行仍停在上一次合法纠错的结果
        row = await _draft_row(db, "record-draft-1")
        assert row["revision"] == 2
        restated = record_draft_from_json(str(row["proposed_record_json"]))
        assert restated == corrected.payload
        assert restated.training_session_id == "s1"  # 归属始终不可经纠错改写
        assert view.payload.training_session_id == "s1"


async def test_stale_revision_and_terminal_states_reject_correction(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _conversation(db)
        service = RecordDraftService(db)
        await _create(db, draft_id="record-draft-1")
        before = await _draft_row(db, "record-draft-1")

        with pytest.raises(DraftRevisionConflict):
            await service.revise_record_draft(
                draft_id="record-draft-1",
                seen_revision=7,
                payload=RecordDraftPayload(
                    occurred_on=OCCURRED_ON,
                    training_session_id=None,
                    exercises=(),
                ),
            )
        assert await _draft_row(db, "record-draft-1") == before

        # 已提交（S3-06 同口径的凭据替身）：终态不可纠错、不可丢弃
        await _create(db, draft_id="record-draft-2")
        async with db.transaction() as conn:
            await DraftRepo(db).record_commit_in_transaction(
                conn,
                draft_id="record-draft-2",
                committed_revision=1,
                committed_business_version=1,
            )
        with pytest.raises(DraftNotCorrectable):
            await service.revise_record_draft(
                draft_id="record-draft-2",
                seen_revision=1,
                payload=RecordDraftPayload(
                    occurred_on=OCCURRED_ON,
                    training_session_id=None,
                    exercises=(_weight_log(),),
                ),
            )

        # 已丢弃（记录草稿的丢弃入口未在 S3-10 工作项内，用共享 repo 条件更新做替身）：
        # 终态不可纠错；未知草稿报未找到，不新建
        await _create(db, draft_id="record-draft-3")
        async with db.transaction() as conn:
            await DraftRepo(db).record_discard_in_transaction(
                conn, draft_id="record-draft-3"
            )
        with pytest.raises(DraftNotCorrectable):
            await service.revise_record_draft(
                draft_id="record-draft-3",
                seen_revision=1,
                payload=RecordDraftPayload(
                    occurred_on=OCCURRED_ON,
                    training_session_id=None,
                    exercises=(_weight_log(),),
                ),
            )
        with pytest.raises(UnknownDraft):
            await service.revise_record_draft(
                draft_id="nope",
                seen_revision=1,
                payload=RecordDraftPayload(
                    occurred_on=OCCURRED_ON,
                    training_session_id=None,
                    exercises=(_weight_log(),),
                ),
            )
        assert await _formal_counts(db) == (0, 0, 0, 0)


# ---------- 重开一致 ----------


async def test_restart_keeps_payload_status_and_diff_consistent(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        await _conversation(db)
        await _seed_confirmed_session(
            db,
            session_id="s1",
            revision_id="r1",
            occurred_on=OCCURRED_ON,
            exercises=(_weight_log(sets=(_weight_set(value="70", reps=6),)),),
        )
        view = await _create(db, training_session_id="s1")
        stored = view.draft.proposed_record_json

    async with open_database(path) as db:
        again = await RecordDraftService(db).get_record_draft("record-draft-1")
        assert again is not None
        assert again.draft.proposed_record_json == stored
        assert again.draft.revision == 1
        assert again.status == view.status
        assert again.payload == view.payload
        assert again.baseline == view.baseline
        assert again.diff == view.diff


# ---------- 纯规则：状态派生与纠错白名单（无 IO） ----------


def test_status_derivation_covers_each_record_type_and_unknowns_stay_incomplete() -> (
    None
):
    incomplete = RecordDraftPayload(
        occurred_on=OCCURRED_ON,
        training_session_id=None,
        exercises=(_weight_log(sets=(_weight_set(set_type=None),)),),
    )
    assert record_draft_status(incomplete) == "incomplete"
    assert (
        record_draft_status(
            RecordDraftPayload(
                occurred_on=OCCURRED_ON, training_session_id=None, exercises=()
            )
        )
        == "incomplete"
    )
    # 计时型：时长明确即有效；自重型：次数明确即有效（RIR／质量／辅助可空）
    timed = RecordDraftPayload(
        occurred_on=OCCURRED_ON,
        training_session_id=None,
        exercises=(
            DraftExerciseLog(
                position=1,
                facts=ExerciseLogFacts(exercise_id="plank", record_type="time"),
                sets=(SetFacts(set_no=1, set_type="work", duration_seconds=60),),
            ),
        ),
    )
    assert record_draft_status(timed) == "valid"
    assert (
        record_draft_status(
            RecordDraftPayload(
                occurred_on=OCCURRED_ON,
                training_session_id=None,
                exercises=(_bodyweight_log(),),
            )
        )
        == "valid"
    )
    # 计时型缺时长仍是待补全（不强填次数）
    assert (
        record_draft_status(
            RecordDraftPayload(
                occurred_on=OCCURRED_ON,
                training_session_id=None,
                exercises=(
                    DraftExerciseLog(
                        position=1,
                        facts=ExerciseLogFacts(exercise_id="plank", record_type="time"),
                        sets=(SetFacts(set_no=1, set_type="work"),),
                    ),
                ),
            )
        )
        == "incomplete"
    )


def test_correction_whitelist_allows_facts_and_rejects_identity_changes() -> None:
    stored = RecordDraftPayload(
        occurred_on=OCCURRED_ON,
        training_session_id="s1",
        arrangement_revision_id="arr-1",
        exercises=(_weight_log(),),
    )
    allowed = RecordDraftPayload(
        occurred_on=OCCURRED_ON,
        training_session_id="s1",
        arrangement_revision_id="arr-1",
        exercises=(_weight_log(sets=(_weight_set(value="65"),)),),
        completion_declared=True,
    )
    validate_record_draft_correction(stored, allowed)  # 不抛即允许

    for payload in (
        RecordDraftPayload(
            occurred_on=OCCURRED_ON,
            training_session_id=None,
            arrangement_revision_id="arr-1",
            exercises=(),
        ),
        RecordDraftPayload(
            occurred_on=OCCURRED_ON,
            training_session_id="s1",
            exercises=(),
        ),
        RecordDraftPayload(
            occurred_on=OCCURRED_ON,
            training_session_id="s1",
            arrangement_revision_id="arr-1",
            exercises=(),
            schema_version=2,
        ),
    ):
        with pytest.raises(InvalidRecordFact):
            validate_record_draft_correction(stored, payload)


def test_payload_json_round_trip_keeps_empty_and_unknown_facts_empty() -> None:
    payload = RecordDraftPayload(
        occurred_on=OCCURRED_ON,
        training_session_id=None,
        exercises=(
            _weight_log(
                warmup_summary_text="递增至 60kg",
                sets=(
                    _weight_set(set_no=1, rir=None, quality_text=None),
                    _weight_set(
                        set_no=2,
                        value="62.5",
                        reps=6,
                        rir=1.0,
                        quality_text=None,
                    ),
                ),
            ),
            _bodyweight_log(position=2),
        ),
        completion_declared=True,
        is_return_phase=True,
        feedback={"pain_report": "not_reported"},
    )
    restored = record_draft_from_json(record_draft_to_json(payload))
    assert restored == payload
    assert restored.exercises[0].sets[0].rir is None
    assert restored.exercises[0].sets[0].quality_text is None
    assert restored.feedback == {"pain_report": "not_reported"}
