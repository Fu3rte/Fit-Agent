"""Stage 1 S1-03：动作目录查询用例——别名候选、变式区分、推荐筛选、停用保留。

验收对照（stage1.md §5 S1-03 验收 1–5）：同一标准动作的别名返回相同身份；别名命中
多个身份保留候选；器械变式／记录口径／负重方式可区分；未检查动作不进推荐候选；停用
后按身份查询仍保留原有语义；目录读写不推进 ``context_version``。

产品种子按 ``tests/test_stage1_actions_seed.py`` 的核对表为准；本模块需要
``recommendable=1`` 或重复别名的场景一律插入**测试虚构动作**（id 前缀 ``test-only-``），
不冒充审核过的产品种子。所有用例只操作 ``tmp_path`` 下的临时文件库。
"""

import json
from pathlib import Path

from domain.actions import rules
from domain.actions.repo import ExerciseRepo
from domain.actions.service import ActionCatalogService
from tests.support import open_database

USER_PROFILE_ROW = (1, None, 0)


async def _insert_exercise(
    conn,
    *,
    exercise_id: str,
    standard_name_zh: str,
    aliases: tuple[str, ...] = (),
    modes: tuple[str, ...] = ("深蹲",),
    equipment_variant: str = "dumbbell",
    record_type: str = "reps_weight",
    load_convention: str | None = "dumbbell_per_hand",
    unilateral: int = 0,
    recommendable: int = 0,
    active: int = 1,
) -> None:
    """插入测试虚构动作；不用于冒充产品种子。"""
    await conn.execute(
        "INSERT INTO exercises (id, standard_name_zh, equipment_variant,"
        " record_type, load_convention, unilateral, recommendable, active,"
        " aliases_json, modes_json, source_ref, attribution, instructions_zh)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            exercise_id,
            standard_name_zh,
            equipment_variant,
            record_type,
            load_convention,
            unilateral,
            recommendable,
            active,
            json.dumps(list(aliases), ensure_ascii=False),
            json.dumps(list(modes), ensure_ascii=False),
            "test-fixture",
            "test fixture",
            "测试用虚构动作说明",
        ),
    )


async def _profile_row(db) -> tuple[object, object, object]:
    async def op(conn):
        async with conn.execute(
            "SELECT id, profile_json, context_version FROM user_profile"
        ) as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 1
        return (rows[0]["id"], rows[0]["profile_json"], rows[0]["context_version"])

    return await db.under_lock(op)


# ---------- 验收 1：别名候选与歧义 ----------


async def test_registered_aliases_resolve_to_the_same_identity(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ActionCatalogService(db)
        for term in (
            "sled 45° leg press",
            "SLED 45° LEG PRESS",
            "  sled 45° leg press  ",
            "sled 45в° leg press",  # 源文件乱码写法同样登记
        ):
            resolution = await service.resolve(term)
            assert not resolution.is_ambiguous, term
            assert [e.id for e in resolution.exercises] == ["leg-press-45"], term
            assert resolution.candidates[0].match_kind == "alias"


async def test_standard_name_resolves_to_canonical_identity(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        resolution = await ActionCatalogService(db).resolve("杠铃背蹲")
        assert [e.id for e in resolution.exercises] == ["barbell-back-squat"]
        assert resolution.candidates[0].match_kind == "standard_name"


async def test_alias_hitting_multiple_identities_keeps_all_candidates(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db, db.transaction() as conn:
        await _insert_exercise(
            conn,
            exercise_id="test-only-alias-a",
            standard_name_zh="测试虚构动作甲",
            aliases=("test shared alias",),
        )
        await _insert_exercise(
            conn,
            exercise_id="test-only-alias-b",
            standard_name_zh="测试虚构动作乙",
            aliases=("test shared alias",),
            equipment_variant="barbell",
            load_convention="barbell_includes_bar_total",
        )

    async with open_database(tmp_path / "app.db") as db:
        resolution = await ActionCatalogService(db).resolve("test shared alias")
        assert resolution.is_ambiguous
        assert [e.id for e in resolution.exercises] == [
            "test-only-alias-a",
            "test-only-alias-b",
        ]
        assert all(c.match_kind == "alias" for c in resolution.candidates)


async def test_unknown_and_partial_terms_return_no_candidates(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ActionCatalogService(db)
        for term in ("barbell full", "squat", "哑铃分腿蹲", "卧推", ""):
            resolution = await service.resolve(term)
            assert resolution.candidates == (), term
            assert not resolution.is_ambiguous


# ---------- 验收 2：器械变式、记录口径与负重方式可区分 ----------


async def test_equipment_variants_are_distinct_identities(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ActionCatalogService(db)
        barbell = await service.resolve("barbell bench press")
        dumbbell = await service.resolve("dumbbell bench press")
        incline = await service.resolve("dumbbell incline bench press")

        assert [e.id for e in barbell.exercises] == ["barbell-bench-press"]
        assert [e.id for e in dumbbell.exercises] == ["dumbbell-bench-press"]
        assert [e.id for e in incline.exercises] == ["dumbbell-incline-bench-press"]

        ids = {e.id for e in barbell.exercises + dumbbell.exercises + incline.exercises}
        assert len(ids) == 3  # 相似名不合并为同一身份
        assert barbell.exercises[0].equipment_variant == "barbell"
        assert dumbbell.exercises[0].equipment_variant == "dumbbell"
        assert barbell.exercises[0].load_convention == "barbell_includes_bar_total"
        assert dumbbell.exercises[0].load_convention == "dumbbell_per_hand"
        # 多归属只发生在跨界高复合型动作：上斜卧推＝水平推＋垂直推
        assert incline.exercises[0].modes == ("水平推", "垂直推")
        assert dumbbell.exercises[0].modes == ("水平推",)


async def test_record_and_load_conventions_cover_decided_kinds_only(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        exercises = await ExerciseRepo(db).list_all()
        assert len(exercises) == 24
        record_types = {exercise.record_type for exercise in exercises}
        assert record_types == {"reps_weight", "reps_bodyweight"}
        assert "time" in rules.RECORD_TYPES  # 计时类型保留但首批无条目
        assert "reps_assisted" not in record_types
        conventions = {
            exercise.load_convention
            for exercise in exercises
            if exercise.load_convention is not None
        }
        assert conventions == {
            "barbell_includes_bar_total",
            "dumbbell_per_hand",
            "machine_pin_displayed_value",
            "plate_loaded_total_excluding_empty",
        }
        # 自重次数型不得虚构负重口径
        for exercise in exercises:
            if exercise.record_type == "reps_bodyweight":
                assert exercise.load_convention is None
                assert exercise.unilateral is False


async def test_unilateral_items_keep_per_side_convention(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ActionCatalogService(db)
        for term, expected_id in (
            ("dumbbell single leg split squat", "bulgarian-split-squat"),
            ("dumbbell one arm bent-over row", "one-arm-dumbbell-row"),
        ):
            exercise = (await service.resolve(term)).exercises[0]
            assert exercise.id == expected_id
            assert exercise.unilateral is True
            assert exercise.load_convention == "dumbbell_per_hand"


# ---------- 验收 3–4：受检推荐与停用保留 ----------


async def test_recommendation_candidates_require_checked_flag(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ActionCatalogService(db)
        assert await service.recommendation_candidates() == ()  # 种子全部未检查

        async with db.transaction() as conn:
            await _insert_exercise(
                conn,
                exercise_id="test-only-checked",
                standard_name_zh="测试虚构动作已检查",
                aliases=("test checked",),
                recommendable=1,
            )
            await _insert_exercise(
                conn,
                exercise_id="test-only-unchecked",
                standard_name_zh="测试虚构动作未检查",
                aliases=("test unchecked",),
                recommendable=0,
            )
        candidates = await service.recommendation_candidates()
        assert [e.id for e in candidates] == ["test-only-checked"]


async def test_stopped_exercise_keeps_semantics_but_leaves_recommendations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db, db.transaction() as conn:
        await _insert_exercise(
            conn,
            exercise_id="test-only-stopped",
            standard_name_zh="测试虚构动作停用",
            aliases=("test stopped",),
            recommendable=1,
        )
    async with open_database(path) as db:
        service = ActionCatalogService(db)
        assert [e.id for e in await service.recommendation_candidates()] == [
            "test-only-stopped"
        ]
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE exercises SET active = 0 WHERE id = 'test-only-stopped'"
            )

    # 停服重开：停用不删除，原有语义（别名／负重口径／可推荐标记）保留
    async with open_database(path) as db:
        service = ActionCatalogService(db)
        stopped = await service.get_by_id("test-only-stopped")
        assert stopped is not None
        assert stopped.active is False
        assert stopped.aliases == ("test stopped",)
        assert stopped.load_convention == "dumbbell_per_hand"
        assert stopped.recommendable is True  # 已检查标记不被停用重置
        assert await service.recommendation_candidates() == ()
        resolution = await service.resolve("test stopped")
        assert [e.id for e in resolution.exercises] == ["test-only-stopped"]


async def test_catalog_reads_never_advance_context_version(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = ActionCatalogService(db)
        await service.resolve("barbell full squat")
        await service.get_by_id("pull-up")
        await service.recommendation_candidates()
        await ExerciseRepo(db).list_all()
        assert await _profile_row(db) == USER_PROFILE_ROW
