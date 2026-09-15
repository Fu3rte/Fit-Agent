"""Stage 1 子任务 02 §4：动作目录 domain/actions —— 稳定 ID、13 项模式词表与写入前口径校验。

对照 02 清单：按新 exercises 表重写最小 schema/repo/rules/service、保留稳定动作 ID 与明确负重
口径、用 13 项词表校验 modes_json、提供列表与按 ID 查询、写入记录前校验动作存在且口径匹配。
旧目录语义（别名、停用、单侧、计划模板、RIR）已删除，其字段不在 Exercise 结构里。

测试只使用 pytest tmp_path 下的独立临时库（与 01 的 test_stage1_data_base 同模式），不 import
tests.support（它依赖已移除的 pydantic_ai）。
"""

import dataclasses
import json
from pathlib import Path

import pytest

from domain.actions.repo import ExerciseRepo
from domain.actions.rules import (
    LOAD_CONVENTIONS,
    MODE_VOCABULARY,
    RECORD_TYPES,
    InvalidMode,
    RecordLoadMismatch,
    UnknownExercise,
    validate_modes,
)
from domain.actions.schema import Exercise, InvalidCatalogRow
from domain.actions.service import ActionCatalogService
from storage.db import Database

#: exercises 列对齐后的完整字段集：多一个旧语义字段即回归（别名／停用／单侧／肌群等已删除）。
EXPECTED_FIELDS = {
    "id",
    "standard_name_zh",
    "equipment_variant",
    "record_type",
    "load_convention",
    "min_load_increment_kg",
    "recommendable",
    "modes",
    "source_ref",
    "attribution",
}


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def test_directory_lists_seed_exercises_aligned_to_new_columns(
    tmp_path: Path,
) -> None:
    """目录全量：24 项种子按新列读取，稳定 ID 与负重口径原样保留。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        exercises = await ActionCatalogService(db).list_all()
        assert len(exercises) == 24
        by_id = {exercise.id: exercise for exercise in exercises}
        assert len(by_id) == 24
        barbell_squat = by_id["barbell-back-squat"]
        assert barbell_squat.standard_name_zh == "杠铃背蹲"
        assert barbell_squat.equipment_variant == "barbell"
        assert barbell_squat.record_type == "reps_weight"
        assert barbell_squat.load_convention == "barbell_includes_bar_total"
        assert barbell_squat.min_load_increment_kg == 2.5
        assert barbell_squat.recommendable is True
        assert barbell_squat.modes == ("深蹲",)
        assert barbell_squat.source_ref == "exercises-dataset:0043"
        assert barbell_squat.attribution == "© Gym visual — https://gymvisual.com/"
        # 自重动作不虚构口径与加重单位。
        pull_up = by_id["pull-up"]
        assert pull_up.record_type == "reps_bodyweight"
        assert pull_up.load_convention is None
        assert pull_up.min_load_increment_kg is None
    finally:
        await db.close()


def test_exercise_structure_carries_no_legacy_catalog_semantics() -> None:
    """结构字段集恰为新列：别名、停用标记、单侧标记、肌群／媒体字段已被删除。"""
    assert {field.name for field in dataclasses.fields(Exercise)} == EXPECTED_FIELDS


async def test_get_by_id_returns_stable_identity_and_none_for_unknown(
    tmp_path: Path,
) -> None:
    """按 ID 查询：命中稳定身份；未收录的 ID 返回 None，不按相似名推断。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ActionCatalogService(db)
        assert (await service.get_by_id("lat-pulldown")).standard_name_zh == "高位下拉"
        assert await service.get_by_id("no-such-exercise") is None
    finally:
        await db.close()


async def test_all_seed_modes_are_inside_the_thirteen_item_vocabulary(
    tmp_path: Path,
) -> None:
    """13 项词表是 modes_json 的唯一取值域：种子逐项非空且全部落在词表内。"""
    assert len(MODE_VOCABULARY) == 13
    assert RECORD_TYPES == ("reps_weight", "reps_bodyweight", "time")
    assert len(LOAD_CONVENTIONS) == 5
    db = await _migrated(tmp_path / "x.db")
    try:
        for exercise in await ExerciseRepo(db).list_all():
            assert exercise.modes, exercise.id
            assert all(mode in MODE_VOCABULARY for mode in exercise.modes), exercise.id
            if exercise.record_type == "reps_weight":
                assert exercise.load_convention in LOAD_CONVENTIONS, exercise.id
            else:
                assert exercise.load_convention is None, exercise.id
    finally:
        await db.close()


async def test_corrupt_modes_row_fails_loudly_on_read(tmp_path: Path) -> None:
    """库内 CHECK 只保证 json_valid：越界模式与非数组 modes_json 必须在读取时大声失败。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO exercises (id, standard_name_zh, equipment_variant,"
                " record_type, load_convention, min_load_increment_kg, recommendable,"
                " modes_json, source_ref, attribution)"
                " VALUES ('x-out-of-vocabulary', '架空动作', 'barbell', 'reps_weight',"
                " 'barbell_includes_bar_total', 2.5, 1, ?, 's', 'a')",
                (json.dumps(["塑形"]),),
            )
        with pytest.raises(InvalidMode):
            await ExerciseRepo(db).list_all()

        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE exercises SET modes_json = ? WHERE id = 'x-out-of-vocabulary'",
                (json.dumps("深蹲"),),
            )
        with pytest.raises(InvalidCatalogRow):
            await ExerciseRepo(db).list_all()
    finally:
        await db.close()


def test_validate_modes_rejects_empty_and_unknown_values() -> None:
    with pytest.raises(InvalidMode):
        validate_modes(())
    with pytest.raises(InvalidMode):
        validate_modes(("深蹲", "塑形"))
    validate_modes(("水平推", "垂直推"))


async def test_record_write_accepts_matching_record_and_load_conventions(
    tmp_path: Path,
) -> None:
    """写入前校验通过：外加负重用同口径，自重／计时型无口径。"""
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ActionCatalogService(db)
        squat = await service.validate_record_write(
            "barbell-back-squat",
            record_type="reps_weight",
            load_convention="barbell_includes_bar_total",
        )
        assert squat.id == "barbell-back-squat"
        pull_up = await service.validate_record_write(
            "pull-up", record_type="reps_bodyweight", load_convention=None
        )
        assert pull_up.record_type == "reps_bodyweight"
    finally:
        await db.close()


async def test_record_write_rejects_mismatched_conventions(tmp_path: Path) -> None:
    """口径不符一律拒绝：口径缺失、与目录不同、自重带口径、记录口径与目录不符。"""
    cases: list[tuple[str, str, str | None]] = [
        # 外加负重型：口径缺失或与目录不同一律拒绝。
        ("barbell-back-squat", "reps_weight", None),
        ("barbell-back-squat", "reps_weight", "dumbbell_per_hand"),
        # 自重型：不得携带任何负重口径。
        ("pull-up", "reps_bodyweight", "barbell_includes_bar_total"),
        ("pull-up", "reps_bodyweight", "dumbbell_per_hand"),
        # 记录口径本身与目录不符。
        ("pull-up", "reps_weight", None),
        ("barbell-back-squat", "reps_bodyweight", None),
        # 目录外口径。
        ("barbell-back-squat", "reps_weight", "unilateral_setting_per_side"),
    ]
    db = await _migrated(tmp_path / "x.db")
    try:
        service = ActionCatalogService(db)
        for exercise_id, record_type, load_convention in cases:
            with pytest.raises(RecordLoadMismatch):
                await service.validate_record_write(
                    exercise_id,
                    record_type=record_type,  # type: ignore[arg-type]
                    load_convention=load_convention,  # type: ignore[arg-type]
                )
    finally:
        await db.close()


async def test_record_write_rejects_unknown_exercise(tmp_path: Path) -> None:
    db = await _migrated(tmp_path / "x.db")
    try:
        with pytest.raises(UnknownExercise):
            await ActionCatalogService(db).validate_record_write(
                "no-such-exercise",
                record_type="reps_bodyweight",
                load_convention=None,
            )
    finally:
        await db.close()
