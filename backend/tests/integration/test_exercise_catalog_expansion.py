# 动作目录扩容（110 / 60 可推荐 / 50 仅记录）的目录契约：迁移后的行数与既有行保持、
# aliases 的形态与唯一性、13 项模式的 A 层覆盖、A/B 边界的记录与计划两侧行为、名称匹配四级优先级。
# 依据：Fit-Agent-exercise-catalog-implementation-plan.md §1–§7、§10，与 §3 的构建脚本校验项。
# 计划事实一律用 tmp_path 下的真实迁移库，不 mock 仓储、不伪造目录行。

import importlib.util
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import ModuleType

import aiosqlite
import pytest

from app.application.agent.contracts import AmbiguousExerciseName
from app.application.agent.run_service import _matched_exercise
from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.domain.actions.rules import MODE_VOCABULARY
from app.domain.actions.schema import Exercise, InvalidCatalogRow
from app.domain.plans.rules import validate_plan_draft
from app.domain.plans.schema import (
    BodyweightRepsPrescription,
    NeedsCalibration,
    PlanDraft,
    PlannedExercise,
    TimedPrescription,
    TrainingDay,
    WeightedRepsPrescription,
)
from app.domain.records.schema import WorkoutSetInput
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.actions_repository import ExerciseRepo

TOTAL = 110
RECOMMENDABLE = 60
RECORD_ONLY = 50
PLAN_DAY = date(2026, 6, 1)
BACKEND_DIR = Path(__file__).resolve().parents[2]

# 既有 27 行的稳定 ID 与 source_ref：006 只补 aliases，行本身不得被改写。
EXISTING_SOURCE_REFS = {
    "barbell-back-squat": "exercises-dataset:0043",
    "leg-press-45": "exercises-dataset:0739",
    "plank": "refactor-log/stage2.md:§3",
    "weighted-pull-up": "exercises-dataset:0841",
}
# A 层与 B 层的代表动作（B 层仅记录：不进计划、但能写训练记录）。
RECORD_ONLY_BODYWEIGHT = "wide-hand-push-up"
RECORD_ONLY_TIMED = "front-plank-with-twist"
RECORD_ONLY_CABLE = "cable-preacher-curl"


def _builder_module() -> ModuleType:
    """加载生成脚本本体：别名规范化与全部校验项的权威实现（测试复用，不另写一套）。"""
    spec = importlib.util.spec_from_file_location(
        "build_exercise_catalog", BACKEND_DIR / "tools" / "build_exercise_catalog.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _builder_module()


@asynccontextmanager
async def _catalog(
    tmp_path: Path,
) -> AsyncIterator[tuple[Database, tuple[Exercise, ...]]]:
    """全新迁移库 + 目录全量（按稳定 ID 排序）。"""
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        yield db, await ExerciseRepo(db).list_all()
    finally:
        await db.close()


async def _raw_alias_columns(db: Database) -> list[str]:
    """直接读回 aliases_json 原始文本：DB 侧 json_valid 约束的落脚点。"""

    async def op(conn: aiosqlite.Connection) -> list[str]:
        async with conn.execute("SELECT aliases_json FROM exercises") as cursor:
            return [str(row["aliases_json"]) for row in await cursor.fetchall()]

    return await db.under_lock(op)


def _one_day_draft(exercise: Exercise) -> PlanDraft:
    """按目录记录口径给该动作配一个单日草案：只验目录规则，不涉及负荷来源。"""
    if exercise.record_type == "reps_weight":
        prescription = WeightedRepsPrescription(
            type="weighted_reps",
            reps_min=8,
            reps_max=12,
            load=NeedsCalibration(status="needs_calibration"),
        )
    elif exercise.record_type == "reps_bodyweight":
        prescription = BodyweightRepsPrescription(
            type="bodyweight_reps", reps_min=8, reps_max=12
        )
    else:
        prescription = TimedPrescription(
            type="timed", duration_seconds_min=30, duration_seconds_max=60
        )
    return PlanDraft(
        goal="目录规则自检",
        starts_on=PLAN_DAY,
        explanation="按目录记录口径生成的最小草案",
        weekly_frequency=1,
        training_days=(
            TrainingDay(
                scheduled_on=PLAN_DAY,
                exercises=(
                    PlannedExercise(
                        exercise_id=exercise.id, sets=3, prescription=prescription
                    ),
                ),
            ),
        ),
    )


async def test_migrated_catalog_holds_110_rows_split_60_and_50(tmp_path: Path) -> None:
    """新库迁移后恰好 110 条动作：60 可推荐、50 仅记录，且既有 27 行原样保留。"""
    async with _catalog(tmp_path) as (_db, catalog):
        assert len(catalog) == TOTAL
        recommendable = [exercise for exercise in catalog if exercise.recommendable]
        assert len(recommendable) == RECOMMENDABLE
        assert len(catalog) - len(recommendable) == RECORD_ONLY
        assert [exercise.id for exercise in catalog] == sorted(
            exercise.id for exercise in catalog
        )

        by_id = {exercise.id: exercise for exercise in catalog}
        for exercise_id, source_ref in EXISTING_SOURCE_REFS.items():
            assert by_id[exercise_id].source_ref == source_ref
            assert by_id[exercise_id].recommendable is True


async def test_aliases_json_is_a_string_array_in_every_row(tmp_path: Path) -> None:
    """每一行的 aliases_json 都是合法 JSON 文本数组：2–5 个非空字符串。"""
    async with _catalog(tmp_path) as (db, _catalog_rows):
        raw_columns = await _raw_alias_columns(db)
        assert len(raw_columns) == TOTAL
        for raw in raw_columns:
            aliases = json.loads(raw)
            assert isinstance(aliases, list)
            assert 2 <= len(aliases) <= 5
            assert all(isinstance(alias, str) and alias for alias in aliases)


def test_alias_column_elements_must_be_non_empty_strings() -> None:
    """非字符串或空字符串的 aliases_json 直接失败，不静默强转。"""
    row = {
        "id": "x",
        "standard_name_zh": "x",
        "aliases_json": '["ok", ""]',
        "equipment_variant": "barbell",
        "record_type": "reps_weight",
        "load_convention": "barbell_includes_bar_total",
        "min_load_increment_kg": 2.5,
        "recommendable": 1,
        "modes_json": '["深蹲"]',
        "source_ref": "x",
        "attribution": "x",
    }
    with pytest.raises(InvalidCatalogRow):
        Exercise.from_row(dict(row, aliases_json='["ok", 7]'))
    with pytest.raises(InvalidCatalogRow):
        Exercise.from_row(row)


async def test_standard_names_are_unique(tmp_path: Path) -> None:
    """中文标准名全局唯一。"""
    async with _catalog(tmp_path) as (_db, catalog):
        names = [exercise.standard_name_zh for exercise in catalog]
        assert len(set(names)) == len(names) == TOTAL


async def test_aliases_are_unique_after_normalization(tmp_path: Path) -> None:
    """aliases 规范化后全局唯一，且不与任何动作的标准名冲突（含自身之外的重复归属）。"""
    async with _catalog(tmp_path) as (_db, catalog):
        name_owner = {
            BUILDER.normalize(exercise.standard_name_zh): exercise.id
            for exercise in catalog
        }
        alias_owner: dict[str, str] = {}
        for exercise in catalog:
            for alias in exercise.aliases:
                key = BUILDER.normalize(alias)
                assert alias_owner.get(key, exercise.id) == exercise.id, (
                    f"alias {alias!r} 规范化后重复归属："
                    f"{alias_owner.get(key)} 与 {exercise.id}"
                )
                alias_owner[key] = exercise.id
                assert name_owner.get(key, exercise.id) == exercise.id, (
                    f"alias {alias!r} 与其他动作标准名重名：{name_owner.get(key)}"
                )


async def test_every_mode_has_at_least_one_recommendable_action(tmp_path: Path) -> None:
    """13 项模式词表每一项都至少有一个 A 层（recommendable=true）动作。"""
    async with _catalog(tmp_path) as (_db, catalog):
        covered = {
            mode
            for exercise in catalog
            if exercise.recommendable
            for mode in exercise.modes
        }
        assert set(MODE_VOCABULARY) <= covered


async def test_recommendable_actions_pass_the_catalog_rules(tmp_path: Path) -> None:
    """A 层动作全部能通过确定性目录规则：动作在册、可推荐、处方记录口径一致。"""
    async with _catalog(tmp_path) as (_db, catalog):
        exercises = {exercise.id: exercise for exercise in catalog}
        recommendable = [
            exercise for exercise in catalog if exercise.recommendable
        ]
        assert len(recommendable) == RECOMMENDABLE
        for exercise in recommendable:
            failures = validate_plan_draft(
                _one_day_draft(exercise),
                exercises=exercises,
                profile_weekly_frequency=1,
            )
            assert failures == (), f"{exercise.id}: {failures}"
            if exercise.record_type == "reps_weight":
                assert exercise.load_convention is not None
                assert exercise.min_load_increment_kg is not None
            else:
                assert exercise.load_convention is None
                assert exercise.min_load_increment_kg is None


async def test_record_only_actions_can_be_written_as_records(tmp_path: Path) -> None:
    """B 层动作可以写入训练记录：自重次数型与计时型都不被目录拒绝。"""
    async with _catalog(tmp_path) as (db, _catalog_rows):
        services = build_services(
            build_repositories(db),
            db,
            SqliteHealthProbe(db, db.path.parent),
        )
        _day, bodyweight_facts = await services.records.validate_record_facts(
            PLAN_DAY,
            (
                WorkoutSetInput(
                    exercise_id=RECORD_ONLY_BODYWEIGHT,
                    set_no=1,
                    reps=12,
                    set_type="work",
                ),
            ),
        )
        assert [fact.exercise_id for fact in bodyweight_facts] == [RECORD_ONLY_BODYWEIGHT]

        _day, timed_facts = await services.records.validate_record_facts(
            PLAN_DAY,
            (
                WorkoutSetInput(
                    exercise_id=RECORD_ONLY_TIMED,
                    set_no=1,
                    reps=None,
                    set_type="work",
                    duration_seconds=45,
                ),
            ),
        )
        assert [fact.exercise_id for fact in timed_facts] == [RECORD_ONLY_TIMED]


async def test_record_only_actions_are_rejected_from_plan_drafts(tmp_path: Path) -> None:
    """B 层动作进入计划时被确定性规则拒绝：exercise_not_recommendable。"""
    async with _catalog(tmp_path) as (_db, catalog):
        exercises = {exercise.id: exercise for exercise in catalog}
        record_only = next(
            exercise for exercise in catalog if exercise.id == RECORD_ONLY_BODYWEIGHT
        )
        failures = validate_plan_draft(
            _one_day_draft(record_only),
            exercises=exercises,
            profile_weekly_frequency=1,
        )
        assert [failure.code for failure in failures] == ["exercise_not_recommendable"]


@pytest.mark.parametrize(
    "name, expected_id",
    [
        # 标准名精确命中
        ("杠铃前蹲", "barbell-front-squat"),
        # 中文 alias 精确命中
        ("高脚杯深蹲", "dumbbell-goblet-squat"),
        # 英文 canonical alias 精确命中
        ("barbell front squat", "barbell-front-squat"),
        # 标准名完整包含，取最长名称
        ("我想问杠铃前蹲怎么做", "barbell-front-squat"),
        # 无匹配
        ("波比跳", None),
    ],
)
async def test_matcher_resolves_names_by_the_fixed_priority(
    tmp_path: Path, name: str, expected_id: str | None
) -> None:
    """名称匹配四级优先级：标准名精确 → alias 精确 → 标准名完整包含 → alias 完整包含（各级取最长名）。"""
    async with _catalog(tmp_path) as (_db, catalog):
        matched = _matched_exercise(catalog, name)
        if expected_id is None:
            assert matched is None
        else:
            assert matched is not None
            assert matched.id == expected_id


async def test_matcher_reports_ambiguity_instead_of_picking_one(tmp_path: Path) -> None:
    """精确阶段命中多个候选时立即报歧义，不猜第一个。"""
    async with _catalog(tmp_path) as (_db, catalog):
        source = catalog[0]
        duplicated = tuple(
            Exercise(
                id=f"{source.id}-copy",
                standard_name_zh=source.standard_name_zh,
                aliases=source.aliases,
                equipment_variant=source.equipment_variant,
                record_type=source.record_type,
                load_convention=source.load_convention,
                min_load_increment_kg=source.min_load_increment_kg,
                recommendable=source.recommendable,
                modes=source.modes,
                source_ref=source.source_ref,
                attribution=source.attribution,
            )
            for _ in range(2)
        )
        with pytest.raises(AmbiguousExerciseName):
            _matched_exercise(duplicated, source.standard_name_zh)


async def test_standard_name_containment_outranks_longer_alias_containment(
    tmp_path: Path,
) -> None:
    """包含阶段分两级：标准名包含非空即定案，更长的 alias 包含不得越级抢中。"""
    async with _catalog(tmp_path) as (_db, catalog):
        cable = next(
            exercise
            for exercise in catalog
            if exercise.id == "cable-seated-rear-lateral-raise"
        )
        assert "绳索坐姿后束飞鸟" in cable.aliases
        assert len("绳索坐姿后束飞鸟") > len("自重俯卧撑")
        matched = _matched_exercise(catalog, "今天练了自重俯卧撑和绳索坐姿后束飞鸟")
        assert matched is not None
        assert matched.id == "push-up"


async def test_knowledge_qa_scope_is_recommendable_while_records_see_all(tmp_path: Path) -> None:
    """知识问答只匹配 A 层；自然语言打卡携带全部 110 条，B 层动作仍可被匹配。"""
    async with _catalog(tmp_path) as (_db, catalog):
        record_only = next(
            exercise for exercise in catalog if exercise.id == RECORD_ONLY_CABLE
        )
        assert record_only.recommendable is False
        recommendable_only = tuple(
            exercise for exercise in catalog if exercise.recommendable
        )
        assert _matched_exercise(recommendable_only, record_only.standard_name_zh) is None
        assert (
            _matched_exercise(catalog, record_only.standard_name_zh).id
            == RECORD_ONLY_CABLE
        )


def test_builder_rejects_alias_that_collides_after_normalization() -> None:
    """多候选 alias（规范化后重名）被构建脚本拒绝，退出非零此前已由脚本保证。"""
    rows = json.loads(BUILDER.CATALOG_PATH.read_text(encoding="utf-8"))
    dataset = BUILDER._load_dataset()
    donor = next(row for row in rows if row["id"] == "barbell-shrug")
    target = next(row for row in rows if row["id"] == "cable-shrug")
    target["aliases"] = [donor["aliases"][0], *target["aliases"][1:]]
    with pytest.raises(BUILDER.CatalogError, match="冲突"):
        BUILDER.validate(rows, dataset)


def test_builder_rejects_legacy_row_without_dataset_source_id() -> None:
    """既有行中只有 plank 允许缺 source_id；其余既有行（如 leg-press-45）必须立即失败。"""
    rows = json.loads(BUILDER.CATALOG_PATH.read_text(encoding="utf-8"))
    equipment = {variant: name for name, variant in BUILDER.EQUIPMENT_VARIANTS.items()}
    dataset = {
        row["source_id"]: {
            "equipment": equipment[row["equipment_variant"]],
            "name": row["canonical_name_en"],
        }
        for row in rows
        if row["source_id"] is not None
    }
    BUILDER.validate(rows, dataset)
    legacy = next(row for row in rows if row["id"] == "leg-press-45")
    legacy["source_id"] = None
    with pytest.raises(BUILDER.CatalogError, match="plank"):
        BUILDER.validate(rows, dataset)
