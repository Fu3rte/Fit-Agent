"""Stage 3 S3-13：复盘存取与 stale（``reviews`` 表、正文／快照／来源修订引用）。

验收对照（stage3.md §5 S3-13；06 6.4、06 验收 10；§8 D6 仅显式请求时生成）：

- 迁移 011 建 ``reviews``（不可改写的 Markdown 正文 + 生成时统计依据快照）与
  ``review_source_revisions``（**精确**来源修订引用，外键保证引用存在）；升级／重开正常。
- 保存 seam 只经内部显式入口：正文非空白、来源修订必须存在且是该训练身份的当前修订；
  非法引用零写入。
- 更正与作废使旧复盘**读**到 stale：正文与快照原样保留（不重写、不覆盖）；重生成追加新行。
- 当前统计不读快照：保存（甚至写入伪造数值）后 PR 现算结果不变。

边界：全部经内部应用层（``RecordDraftService``／``ConfirmService``／``ReviewStore``）与
``tmp_path`` 临时文件库，不接 HTTP、不触碰真实用户数据目录；用例里的原始 SQL 只用于读取校验。
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.review_store import ReviewStore
from domain.stats.rules import (
    InvalidReviewContent,
    review_basis_changed,
    validate_review_content,
)
from domain.stats.schema import (
    InvalidReviewRow,
    PrValue,
    ReviewSourceRevisions,
    ReviewStatSnapshot,
    WeekCompletion,
    review_basis_from_json,
    review_basis_to_json,
)
from domain.stats.service import StatsService
from storage.db import Database
from storage.migrations import DEFAULT_MIGRATIONS_DIR, load_migrations
from tests.support import open_database
from tests.test_stage3_arrangement_confirm import (
    _confirm_arrangement,
    _create_arrangement,
    _profile_and_plan,
    _push_session,
)
from tests.test_stage3_record_confirm import _confirm, _void
from tests.test_stage3_record_drafts import (
    _create as _create_record,
)
from tests.test_stage3_record_drafts import (
    _weight_log,
    _weight_set,
)
from tests.test_stage3_stats import PUSH_ON

REVIEWS_MIGRATION_FILE = "011_stage3_reviews.sql"
STAGE3_REVIEW_TABLES = {"reviews", "review_source_revisions"}
BENCH_EXERCISE_ID = "barbell-bench-press"
BARBELL_TOTAL = "barbell_includes_bar_total"

_COUNT_SQL = {
    "reviews": "SELECT COUNT(*) AS n FROM reviews",
    "review_source_revisions": "SELECT COUNT(*) AS n FROM review_source_revisions",
}


# ---------- 原始读取与集成夹具 ----------


async def _fetch(db: Database, sql: str, params: tuple[object, ...] = ()) -> list[dict]:
    """只读原始行（SQL 一律字面量写在用例里，表名不参与拼接）。"""

    async def op(conn):
        async with conn.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _count(db: Database, table: str) -> int:
    rows = await _fetch(db, _COUNT_SQL[table])
    return int(rows[0]["n"])


async def _table_names(db: Database) -> set[str]:
    async def op(conn):
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            return {str(row["name"]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


async def _review_row(db: Database, review_id: str) -> dict:
    rows = await _fetch(
        db,
        "SELECT body_markdown, basis_json, generated_at FROM reviews WHERE id = ?",
        (review_id,),
    )
    assert rows, f"复盘行不存在：{review_id}"
    return rows[0]


async def _confirmed_session(
    db: Database, *, draft_id: str = "record-draft-1"
) -> tuple[str, str]:
    """经真实链路建立一条已确认训练：返回 (训练身份 id, 当前修订 id)。

    复用 S3-08／S3-11 的真实入口：确认计划 → 接受当次安排 → 记录草稿确认，
    因此记录绑定**执行时所依据**的安排修订，不是测试替身。
    """
    plan = await _profile_and_plan(db)
    session = await _push_session(db, plan.id)
    await _create_arrangement(db, draft_id="arr-draft-1", session_id=session.id)
    await _confirm_arrangement(db, draft_id="arr-draft-1")
    await _create_record(
        db,
        draft_id=draft_id,
        occurred_on=PUSH_ON,
        exercises=(_weight_log(position=1, sets=(_weight_set(),)),),
    )
    result = await _confirm(db, draft_id=draft_id)
    assert result.session_revision_id is not None
    return result.training_session_id, result.session_revision_id


def _basis(plan_version_id: str = "pv-1") -> ReviewStatSnapshot:
    return ReviewStatSnapshot(
        per_week=(
            WeekCompletion(
                plan_version_id=plan_version_id,
                week_no=1,
                week_start=PUSH_ON,
                week_end=date(2026, 9, 21),
                numerator=1,
                denominator=3,
            ),
        ),
        prs=(
            PrValue(
                exercise_id=BENCH_EXERCISE_ID,
                load_notation=BARBELL_TOTAL,
                load_kg_key=100000,
                best_reps=8,
            ),
        ),
    )


# ---------- 领域规则（纯函数，不碰库） ----------


@pytest.mark.parametrize(
    ("references", "expected"),
    [
        # 所引修订就是该训练身份的当前修订：依据未变
        ((ReviewSourceRevisions("rev-1", "ts-1", "rev-1"),), False),
        # 更正追加新修订并切换指针：所引修订不再是当前
        ((ReviewSourceRevisions("rev-1", "ts-1", "rev-2"),), True),
        # 作废同样追加 voided 修订并切换（05 5.3）
        ((ReviewSourceRevisions("rev-1", "ts-1", "rev-void"),), True),
        # 多笔来源：任一笔变更即为已变更
        (
            (
                ReviewSourceRevisions("rev-1", "ts-1", "rev-1"),
                ReviewSourceRevisions("rev-2", "ts-2", "rev-3"),
            ),
            True,
        ),
        # 指针被清空（库内损坏态）同样不是「仍是当前」
        ((ReviewSourceRevisions("rev-1", "ts-1", None),), True),
        # 没有来源修订：无依据可比，不判为已变更
        ((), False),
    ],
)
def test_review_basis_changed_table(
    references: tuple[ReviewSourceRevisions, ...], expected: bool
) -> None:
    assert review_basis_changed(references) is expected


@pytest.mark.parametrize("body", ["", "   \n\t ", 42, None])
def test_validate_review_content_rejects_blank_markdown(body: object) -> None:
    with pytest.raises(InvalidReviewContent):
        validate_review_content(body_markdown=body, source_revision_ids=())  # type: ignore[arg-type]


def test_validate_review_content_rejects_duplicate_or_non_text_references() -> None:
    validate_review_content(body_markdown="# 复盘", source_revision_ids=("rev-1",))
    with pytest.raises(InvalidReviewContent, match="不得重复"):
        validate_review_content(
            body_markdown="# 复盘", source_revision_ids=("rev-1", "rev-1")
        )
    with pytest.raises(InvalidReviewContent, match="非空文本"):
        validate_review_content(body_markdown="# 复盘", source_revision_ids=("",))


def test_review_basis_json_round_trip_and_loud_failures() -> None:
    basis = _basis()
    assert review_basis_from_json(review_basis_to_json(basis)) == basis
    # 空快照也合法（该次复盘没有任何现算数值）
    empty = ReviewStatSnapshot()
    assert review_basis_from_json(review_basis_to_json(empty)) == empty

    bad_payloads = (
        "not json",
        "[]",
        json.dumps({"schema_version": 2, "per_week": [], "prs": []}),
        json.dumps(
            {"schema_version": 1, "per_week": [], "prs": [{"exercise_id": "x"}]}
        ),
        json.dumps(
            {
                "schema_version": 1,
                "per_week": [
                    {
                        "plan_version_id": "pv-1",
                        "week_no": True,
                        "week_start": "2026-09-14",
                        "week_end": "2026-09-21",
                        "numerator": 0,
                        "denominator": 0,
                    }
                ],
                "prs": [],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "per_week": [
                    {
                        "plan_version_id": "pv-1",
                        "week_no": 1,
                        "week_start": "not-a-date",
                        "week_end": "2026-09-21",
                        "numerator": 0,
                        "denominator": 0,
                    }
                ],
                "prs": [],
            }
        ),
    )
    for payload in bad_payloads:
        with pytest.raises(InvalidReviewRow):
            review_basis_from_json(payload)


# ---------- 迁移 011：复盘两表 ----------


async def test_migration_011_creates_review_tables_only(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        assert await db.migrate() == len(load_migrations())
        assert await db.pragma_value("user_version") == len(load_migrations())
        assert await _table_names(db) >= STAGE3_REVIEW_TABLES
        # 两表都空、且没有第二套版本计数器
        assert await _count(db, "reviews") == 0
        assert await _count(db, "review_source_revisions") == 0

        assert await _column_names(db, "reviews") == {
            "id",
            "body_markdown",
            "basis_json",
            "generated_at",
        }
        assert "context_version" not in await _column_names(db, "reviews")
        # 011 不建统计结果表：当前统计仍一律现算（06 6.3）
        assert await _object_sql(db, kind="view", name="pr_candidates") is not None
        assert "personal_records" not in await _table_names(db)


async def _columns(conn, table: str) -> set[str]:
    async with conn.execute(
        "SELECT name FROM pragma_table_info(?)", (table,)
    ) as cursor:
        return {str(row["name"]) for row in await cursor.fetchall()}


async def _column_names(db: Database, table: str) -> set[str]:
    async def op(conn):
        return await _columns(conn, table)

    return await db.under_lock(op)


async def _object_sql(db: Database, *, kind: str, name: str) -> str | None:
    async def op(conn):
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row["sql"])

    return await db.under_lock(op)


async def test_migration_011_upgrades_existing_database_and_survives_restart(
    tmp_path: Path,
) -> None:
    """已建库（含训练事实）升级到 011：旧事实保留、复盘两表就位，关掉重开不重跑。"""
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        session_id, revision_id = await _confirmed_session(db)
        await ReviewStore(db).save_review(
            body_markdown="# 第 1 周复盘",
            basis=_basis(),
            source_revision_ids=(revision_id,),
        )

    async with open_database(path) as db:
        assert await db.migrate() == len(load_migrations())  # 无新迁移可执行
        # 若 011 被重复执行，CREATE TABLE 会冲突；不抛错且正文仍在即证明跳过
        assert await _count(db, "reviews") == 1
        assert await _count(db, "review_source_revisions") == 1
        rows = await _fetch(db, "SELECT id FROM training_sessions")
        assert [row["id"] for row in rows] == [session_id]


async def test_review_tables_reject_invalid_rows_at_the_schema_level(
    tmp_path: Path,
) -> None:
    """库层拒绝空正文、坏 JSON、重复引用与不存在的引用（不放行半条）。"""
    import sqlite3

    async with open_database(tmp_path / "app.db") as db:
        session_id, revision_id = await _confirmed_session(db)

        async def op(conn):
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO reviews (id, body_markdown, basis_json, generated_at)"
                    " VALUES ('rv-blank', '   ', '{}', 'stamp')"
                )
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    "INSERT INTO reviews (id, body_markdown, basis_json, generated_at)"
                    " VALUES ('rv-json', '# x', 'not json', 'stamp')"
                )
            await conn.execute(
                "INSERT INTO reviews (id, body_markdown, basis_json, generated_at)"
                " VALUES ('rv-1', '# x', '{}', 'stamp')"
            )
            with pytest.raises(sqlite3.IntegrityError):  # 引用不存在的复盘
                await conn.execute(
                    "INSERT INTO review_source_revisions (review_id, session_revision_id)"
                    " VALUES ('rv-missing', ?)",
                    (revision_id,),
                )
            with pytest.raises(sqlite3.IntegrityError):  # 引用不存在的修订
                await conn.execute(
                    "INSERT INTO review_source_revisions (review_id, session_revision_id)"
                    " VALUES ('rv-1', 'rev-missing')"
                )
            await conn.execute(
                "INSERT INTO review_source_revisions (review_id, session_revision_id)"
                " VALUES ('rv-1', ?)",
                (revision_id,),
            )
            with pytest.raises(sqlite3.IntegrityError):  # 同一复盘重复引用同一修订
                await conn.execute(
                    "INSERT INTO review_source_revisions (review_id, session_revision_id)"
                    " VALUES ('rv-1', ?)",
                    (revision_id,),
                )

        await db.under_lock(op)
        # 非法行未留下半条：只有造出的那一条合法复盘与一条合法引用
        assert await _count(db, "reviews") == 1
        assert await _count(db, "review_source_revisions") == 1

        # 复盘不新增业务版本计数器（唯一版本仍是 user_profile.context_version）
        async with db.transaction() as conn:
            await conn.execute("SELECT COUNT(*) FROM training_sessions")
        assert await _fetch(db, "SELECT context_version FROM user_profile WHERE id = 1")


async def test_reviews_migration_is_contiguous_after_pr_candidates_view() -> None:
    """编号连续：011 紧接 010，且不改既有表的写入口径。"""
    names = sorted(path.name for path in DEFAULT_MIGRATIONS_DIR.glob("*.sql"))
    position = names.index(REVIEWS_MIGRATION_FILE)
    assert (
        int(names[position].split("_", 1)[0])
        == int(names[position - 1].split("_", 1)[0]) + 1
    )
    sql = (DEFAULT_MIGRATIONS_DIR / REVIEWS_MIGRATION_FILE).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    assert "user_profile" not in body
    assert "context_version" not in body
    assert "UPDATE" not in body.upper()  # 复盘只追加，不改写
    assert "DELETE" not in body.upper()


# ---------- 保存 seam：精确引用、非法引用零写入 ----------


async def test_save_review_persists_markdown_snapshot_and_exact_references(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        session_id, revision_id = await _confirmed_session(db)
        basis = _basis()
        saved = await ReviewStore(db).save_review(
            body_markdown="# 第 1 周复盘\n\n卧推 100kg×8 是本周期最好成绩。",
            basis=basis,
            source_revision_ids=(revision_id,),
        )
        assert saved.stale is False  # 来源刚校验为当前修订

        view = await ReviewStore(db).read_review(saved.id)
        assert view is not None
        assert view.body_markdown == saved.body_markdown
        assert view.basis == basis
        assert view.source_revision_ids == (revision_id,)
        assert view.stale is False
        # 存的是精确修订 id，不是「该训练的最新修订」指针
        rows = await _fetch(
            db,
            "SELECT ref.session_revision_id, s.current_revision_id"
            " FROM review_source_revisions AS ref"
            " JOIN session_revisions AS r ON r.id = ref.session_revision_id"
            " JOIN training_sessions AS s ON s.id = r.session_id WHERE ref.review_id = ?",
            (saved.id,),
        )
        assert rows == [
            {"session_revision_id": revision_id, "current_revision_id": revision_id}
        ]
        assert await _fetch(db, "SELECT id FROM training_sessions") == [
            {"id": session_id}
        ]

    # 关掉重开：正文、快照与来源引用都还在，stale 仍为假（重新读库计算）
    async with open_database(path) as db:
        view = await ReviewStore(db).read_review(saved.id)
        assert view is not None and view.stale is False
        assert view.basis == basis
        assert view.body_markdown == saved.body_markdown


async def test_save_review_rejects_invalid_content_without_writing(
    tmp_path: Path,
) -> None:
    """非法内容一律拒结且零写入（空正文 / 不存在的修订 / 重复引用）。"""
    cases = (
        ("", (), "非空白"),
        ("  \n", (), "非空白"),
        ("# 复盘", ("missing",), "不存在"),
        ("# 复盘", ("current", "current"), "不得重复"),
    )
    async with open_database(tmp_path / "app.db") as db:
        _, revision_id = await _confirmed_session(db)
        for body, references, match in cases:
            given = tuple(
                revision_id if item == "current" else item for item in references
            )
            with pytest.raises(InvalidReviewContent, match=match):
                await ReviewStore(db).save_review(
                    body_markdown=body, basis=_basis(), source_revision_ids=given
                )
        assert await _count(db, "reviews") == 0
        assert await _count(db, "review_source_revisions") == 0


async def test_save_review_rejects_revision_that_is_no_longer_current(
    tmp_path: Path,
) -> None:
    """指向旧修订的引用被拒：否则新存的复盘一落库就 stale（引用必须精确且当前）。"""
    async with open_database(tmp_path / "app.db") as db:
        session_id, first_revision = await _confirmed_session(db)
        # 更正：向同一身份追加新修订并切换当前指针（S3-11）
        await _create_record(
            db,
            draft_id="record-draft-2",
            occurred_on=PUSH_ON,
            exercises=(_weight_log(position=1, sets=(_weight_set(reps=6),)),),
            training_session_id=session_id,
        )
        corrected = await _confirm(db, draft_id="record-draft-2")
        assert corrected.session_revision_id != first_revision

        with pytest.raises(InvalidReviewContent, match="已不是该训练的当前修订"):
            await ReviewStore(db).save_review(
                body_markdown="# 复盘",
                basis=_basis(),
                source_revision_ids=(first_revision,),
            )
        assert await _count(db, "reviews") == 0
        # 指向新修订（当前）通过
        saved = await ReviewStore(db).save_review(
            body_markdown="# 复盘",
            basis=_basis(),
            source_revision_ids=(corrected.session_revision_id,),
        )
        assert saved.stale is False


# ---------- stale：更正／作废使旧复盘读到 stale，正文与快照不重写 ----------


async def test_correction_makes_old_review_read_stale_without_rewriting_it(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        session_id, revision_id = await _confirmed_session(db)
        store = ReviewStore(db)
        saved = await store.save_review(
            body_markdown="# 第 1 周复盘：卧推 60kg×8",
            basis=_basis(),
            source_revision_ids=(revision_id,),
        )
        before = await _review_row(db, saved.id)

        # 更正：同一次训练的修订追加（这里把 60kg×8 改成 60kg×6）
        await _create_record(
            db,
            draft_id="record-draft-2",
            occurred_on=PUSH_ON,
            exercises=(_weight_log(position=1, sets=(_weight_set(reps=6),)),),
            training_session_id=session_id,
        )
        corrected = await _confirm(db, draft_id="record-draft-2")

        after = await store.read_review(saved.id)
        assert after is not None
        assert after.stale is True  # 依据已变更：可重新生成
        # 旧正文与旧快照原样保留（不静默改写）
        assert after.body_markdown == saved.body_markdown
        assert after.basis == saved.basis
        assert await _review_row(db, saved.id) == before  # 连行字节都没动
        # 来源引用仍指向生成时那一笔，未被改写成「当前修订」
        assert after.source_revision_ids == (revision_id,)
        assert corrected.session_revision_id != revision_id


async def test_void_makes_old_review_read_stale(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        session_id, revision_id = await _confirmed_session(db)
        store = ReviewStore(db)
        saved = await store.save_review(
            body_markdown="# 含该次训练的复盘",
            basis=_basis(),
            source_revision_ids=(revision_id,),
        )
        assert (await store.read_review(saved.id)).stale is False  # type: ignore[union-attr]

        # 作废：追加 voided 修订并切换当前指针（05 5.3），整次退出统计
        await _create_record(
            db,
            draft_id="record-draft-void",
            occurred_on=PUSH_ON,
            exercises=(_weight_log(position=1, sets=(_weight_set(),)),),
            training_session_id=session_id,
        )
        await _void(db, draft_id="record-draft-void")

        view = await store.read_review(saved.id)
        assert view is not None and view.stale is True
        assert view.body_markdown == saved.body_markdown


async def test_regeneration_appends_and_keeps_both_reviews_readable(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        session_id, revision_id = await _confirmed_session(db)
        store = ReviewStore(db)
        first = await store.save_review(
            body_markdown="# 第 1 版复盘",
            basis=_basis(),
            source_revision_ids=(revision_id,),
        )
        first_row = await _review_row(db, first.id)

        # 更正后重新生成：追加新行，旧行原样保留（不覆盖）
        await _create_record(
            db,
            draft_id="record-draft-2",
            occurred_on=PUSH_ON,
            exercises=(_weight_log(position=1, sets=(_weight_set(reps=6),)),),
            training_session_id=session_id,
        )
        corrected = await _confirm(db, draft_id="record-draft-2")
        second = await store.save_review(
            body_markdown="# 第 2 版复盘（依据已更新）",
            basis=_basis(),
            source_revision_ids=(corrected.session_revision_id,),
        )
        assert second.id != first.id

        views = await store.list_reviews()
        assert [view.id for view in views] == [first.id, second.id]
        assert views[0].stale is True  # 旧版标记 stale，可重新生成
        assert views[1].stale is False
        assert views[0].body_markdown == "# 第 1 版复盘"
        assert views[1].body_markdown == "# 第 2 版复盘（依据已更新）"
        assert await _review_row(db, first.id) == first_row  # 旧行未被改写
        assert await _count(db, "reviews") == 2
        assert await _count(db, "review_source_revisions") == 2


# ---------- 当前统计不读快照 ----------


async def test_current_stats_ignore_review_snapshots(tmp_path: Path) -> None:
    """保存复盘（哪怕快照里写伪造数值）不改变任何现算统计（06 6.3／验收 10）。"""
    async with open_database(tmp_path / "app.db") as db:
        session_id, revision_id = await _confirmed_session(db)
        stats = StatsService(db)
        pr_before = await stats.pr_max_load(
            exercise_id=BENCH_EXERCISE_ID, load_notation=BARBELL_TOTAL
        )
        week_before = await stats.weekly_completion(
            "pv-placeholder", 1, business_date=PUSH_ON
        )

        fake = ReviewStatSnapshot(
            per_week=(
                WeekCompletion(
                    plan_version_id="pv-placeholder",
                    week_no=1,
                    week_start=PUSH_ON,
                    week_end=date(2026, 9, 21),
                    numerator=99,
                    denominator=99,
                ),
            ),
            prs=(
                PrValue(
                    exercise_id=BENCH_EXERCISE_ID,
                    load_notation=BARBELL_TOTAL,
                    load_kg_key=999000,
                    best_reps=99,
                ),
            ),
        )
        await ReviewStore(db).save_review(
            body_markdown="# 快照里的数字是生成时的，不是当前统计",
            basis=fake,
            source_revision_ids=(revision_id,),
        )

        # 现算结果与保存前一致：快照没有被回填进统计查询
        assert (
            await stats.pr_max_load(
                exercise_id=BENCH_EXERCISE_ID, load_notation=BARBELL_TOTAL
            )
            == pr_before
        )
        assert (
            await stats.weekly_completion("pv-placeholder", 1, business_date=PUSH_ON)
            == week_before
        )
        assert pr_before != fake.prs[0].load_kg_key
        # 复盘表的存在不改变记录侧的当前事实
        assert await _fetch(
            db, "SELECT current_revision_id FROM training_sessions"
        ) == [{"current_revision_id": revision_id}]
        assert await _fetch(db, "SELECT id FROM training_sessions") == [
            {"id": session_id}
        ]


def test_review_store_module_has_no_generation_or_http_dependency() -> None:
    """S3-13 只存取：不生成模型正文、不接 HTTP（D6；触发生成归显式请求方／Stage 4）。"""
    source = (
        Path(__file__).resolve().parents[1] / "app" / "review_store.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "openai",
        "pydantic_ai",
        "fastapi",
        "routes_",
        "StreamingResponse",
    ):
        assert forbidden not in source
    # 与统计域共用规则：读旧复盘不改写、也不重算
    assert "stats.service" not in source
