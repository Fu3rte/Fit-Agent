"""Stage 1 S1-03：产品种子逐项核对与编号迁移维护（003 只 INSERT）。

验收对照（stage1.md §5 S1-03）：记录类型／负重口径／模式归属逐条核对来源；种子由
编号迁移增量维护，重复导入不制造重复身份或覆盖停用状态；种子导入不修改用户档案或
``context_version``；003 种子写入 ``recommendable=0``，Stage 3 S3-02 的系统迁移（007）
再把已核对 24 项置 1（D2 A）。

``SEED_ROWS`` 是**逐项来源核对表**（核对依据：数据集 instructions.zh / equipment /
target 与动作定义一致，不是名称相似）；证据文件
``pre-prj/stage/evidence/S1-evidence-2026-09-09.md`` 同步记录同一张表。
所有用例只操作 ``tmp_path`` 下的临时文件库与临时迁移目录。
"""

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from domain.actions import rules
from domain.actions.repo import ExerciseRepo
from domain.actions.service import ActionCatalogService
from storage.errors import MigrationError
from storage.migrations import DEFAULT_MIGRATIONS_DIR, load_migrations
from tests.support import open_database

SEEDED_EXERCISE_COUNT = (
    24  # 已拍 24 项清单全部导入（2026-09-09 拍定移除「哑铃分腿蹲」）
)
ATTRIBUTION = "© Gym visual — https://gymvisual.com/"
SEED_MIGRATION_FILE = "003_stage1_action_seed.sql"
USER_PROFILE_ROW = (1, None, 0)

# 逐项核对结果：动作身份、数据集来源 ID、器械变式、记录口径、负重口径、单侧、
# 别名（数据集英文 name；0739 另有源文件乱码名一并保留）。
SEED_ROWS: tuple[dict[str, object], ...] = (
    {
        "id": "barbell-back-squat",
        "standard_name_zh": "杠铃背蹲",
        "source_id": "0043",
        "source_name": "barbell full squat",
        "equipment_variant": "barbell",
        "record_type": "reps_weight",
        "load_convention": "barbell_includes_bar_total",
        "unilateral": 0,
        "aliases": ("barbell full squat",),
    },
    {
        "id": "barbell-deadlift",
        "standard_name_zh": "杠铃传统硬拉",
        "source_id": "0032",
        "source_name": "barbell deadlift",
        "equipment_variant": "barbell",
        "record_type": "reps_weight",
        "load_convention": "barbell_includes_bar_total",
        "unilateral": 0,
        "aliases": ("barbell deadlift",),
    },
    {
        "id": "barbell-romanian-deadlift",
        "standard_name_zh": "杠铃罗马尼亚硬拉",
        "source_id": "0085",
        "source_name": "barbell romanian deadlift",
        "equipment_variant": "barbell",
        "record_type": "reps_weight",
        "load_convention": "barbell_includes_bar_total",
        "unilateral": 0,
        "aliases": ("barbell romanian deadlift",),
    },
    {
        "id": "leg-press-45",
        "standard_name_zh": "45°腿举",
        "source_id": "0739",
        "source_name": "sled 45в° leg press",
        "equipment_variant": "sled_machine",
        "record_type": "reps_weight",
        "load_convention": "plate_loaded_total_excluding_empty",
        "unilateral": 0,
        # 数据集 name 含乱码（в°）；保留规范化写法 + 源文件原样，便于两种输入命中。
        "aliases": ("sled 45° leg press", "sled 45в° leg press"),
    },
    {
        "id": "bulgarian-split-squat",
        "standard_name_zh": "保加利亚分腿蹲",
        "source_id": "0410",
        "source_name": "dumbbell single leg split squat",
        "equipment_variant": "dumbbell",
        "record_type": "reps_weight",
        "load_convention": "dumbbell_per_hand",
        "unilateral": 1,
        "aliases": ("dumbbell single leg split squat",),
    },
    {
        "id": "barbell-bench-press",
        "standard_name_zh": "杠铃平板卧推",
        "source_id": "0025",
        "source_name": "barbell bench press",
        "equipment_variant": "barbell",
        "record_type": "reps_weight",
        "load_convention": "barbell_includes_bar_total",
        "unilateral": 0,
        "aliases": ("barbell bench press",),
    },
    {
        "id": "dumbbell-bench-press",
        "standard_name_zh": "哑铃平板卧推",
        "source_id": "0289",
        "source_name": "dumbbell bench press",
        "equipment_variant": "dumbbell",
        "record_type": "reps_weight",
        "load_convention": "dumbbell_per_hand",
        "unilateral": 0,
        "aliases": ("dumbbell bench press",),
    },
    {
        "id": "dumbbell-incline-bench-press",
        "standard_name_zh": "哑铃上斜卧推",
        "source_id": "0314",
        "source_name": "dumbbell incline bench press",
        "equipment_variant": "dumbbell",
        "record_type": "reps_weight",
        "load_convention": "dumbbell_per_hand",
        "unilateral": 0,
        "aliases": ("dumbbell incline bench press",),
    },
    {
        "id": "seated-dumbbell-shoulder-press",
        "standard_name_zh": "坐姿哑铃肩推",
        "source_id": "0405",
        "source_name": "dumbbell seated shoulder press",
        "equipment_variant": "dumbbell",
        "record_type": "reps_weight",
        "load_convention": "dumbbell_per_hand",
        "unilateral": 0,
        "aliases": ("dumbbell seated shoulder press",),
    },
    {
        "id": "dumbbell-lateral-raise",
        "standard_name_zh": "哑铃侧平举",
        "source_id": "0334",
        "source_name": "dumbbell lateral raise",
        "equipment_variant": "dumbbell",
        "record_type": "reps_weight",
        "load_convention": "dumbbell_per_hand",
        "unilateral": 0,
        "aliases": ("dumbbell lateral raise",),
    },
    {
        "id": "dumbbell-reverse-fly",
        "standard_name_zh": "哑铃反向飞鸟",
        "source_id": "0383",
        "source_name": "dumbbell reverse fly",
        "equipment_variant": "dumbbell",
        "record_type": "reps_weight",
        "load_convention": "dumbbell_per_hand",
        "unilateral": 0,
        "aliases": ("dumbbell reverse fly",),
    },
    {
        "id": "barbell-bent-over-row",
        "standard_name_zh": "杠铃俯身划船",
        "source_id": "0027",
        "source_name": "barbell bent over row",
        "equipment_variant": "barbell",
        "record_type": "reps_weight",
        "load_convention": "barbell_includes_bar_total",
        "unilateral": 0,
        "aliases": ("barbell bent over row",),
    },
    {
        "id": "seated-cable-row",
        "standard_name_zh": "坐姿绳索划船",
        "source_id": "0861",
        "source_name": "cable seated row",
        "equipment_variant": "cable",
        "record_type": "reps_weight",
        "load_convention": "machine_pin_displayed_value",
        "unilateral": 0,
        "aliases": ("cable seated row",),
    },
    {
        "id": "one-arm-dumbbell-row",
        "standard_name_zh": "单臂哑铃划船",
        "source_id": "0292",
        "source_name": "dumbbell one arm bent-over row",
        "equipment_variant": "dumbbell",
        "record_type": "reps_weight",
        "load_convention": "dumbbell_per_hand",
        "unilateral": 1,
        "aliases": ("dumbbell one arm bent-over row",),
    },
    {
        "id": "lat-pulldown",
        "standard_name_zh": "高位下拉",
        "source_id": "0198",
        "source_name": "cable pulldown",
        "equipment_variant": "cable",
        "record_type": "reps_weight",
        "load_convention": "machine_pin_displayed_value",
        "unilateral": 0,
        "aliases": ("cable pulldown",),
    },
    {
        "id": "pull-up",
        "standard_name_zh": "自重引体向上",
        "source_id": "0652",
        "source_name": "pull-up",
        "equipment_variant": "bodyweight",
        "record_type": "reps_bodyweight",
        "load_convention": None,
        "unilateral": 0,
        "aliases": ("pull-up",),
    },
    {
        "id": "seated-leg-curl",
        "standard_name_zh": "坐姿腿弯举",
        "source_id": "0599",
        "source_name": "lever seated leg curl",
        "equipment_variant": "leverage_machine",
        "record_type": "reps_weight",
        "load_convention": "machine_pin_displayed_value",
        "unilateral": 0,
        "aliases": ("lever seated leg curl",),
    },
    {
        "id": "leg-extension",
        "standard_name_zh": "腿屈伸",
        "source_id": "0585",
        "source_name": "lever leg extension",
        "equipment_variant": "leverage_machine",
        "record_type": "reps_weight",
        "load_convention": "machine_pin_displayed_value",
        "unilateral": 0,
        "aliases": ("lever leg extension",),
    },
    {
        "id": "machine-standing-calf-raise",
        "standard_name_zh": "器械站姿提踵",
        "source_id": "0605",
        "source_name": "lever standing calf raise",
        "equipment_variant": "leverage_machine",
        "record_type": "reps_weight",
        "load_convention": "machine_pin_displayed_value",
        "unilateral": 0,
        "aliases": ("lever standing calf raise",),
    },
    {
        "id": "dumbbell-biceps-curl",
        "standard_name_zh": "哑铃弯举",
        "source_id": "0294",
        "source_name": "dumbbell biceps curl",
        "equipment_variant": "dumbbell",
        "record_type": "reps_weight",
        "load_convention": "dumbbell_per_hand",
        "unilateral": 0,
        "aliases": ("dumbbell biceps curl",),
    },
    {
        "id": "cable-pushdown",
        "standard_name_zh": "绳索下压",
        "source_id": "0201",
        "source_name": "cable pushdown",
        "equipment_variant": "cable",
        "record_type": "reps_weight",
        "load_convention": "machine_pin_displayed_value",
        "unilateral": 0,
        "aliases": ("cable pushdown",),
    },
    {
        "id": "cable-overhead-triceps-extension",
        "standard_name_zh": "绳索过顶臂屈伸",
        "source_id": "0194",
        "source_name": "cable overhead triceps extension (rope attachment)",
        "equipment_variant": "cable",
        "record_type": "reps_weight",
        "load_convention": "machine_pin_displayed_value",
        "unilateral": 0,
        "aliases": ("cable overhead triceps extension (rope attachment)",),
    },
    {
        "id": "parallel-bar-dip",
        "standard_name_zh": "自重双杠臂屈伸",
        "source_id": "0251",
        "source_name": "chest dip",
        "equipment_variant": "bodyweight",
        "record_type": "reps_bodyweight",
        "load_convention": None,
        "unilateral": 0,
        "aliases": ("chest dip",),
    },
    {
        "id": "hanging-leg-raise",
        "standard_name_zh": "悬垂举腿",
        "source_id": "0472",
        "source_name": "hanging leg raise",
        "equipment_variant": "bodyweight",
        "record_type": "reps_bodyweight",
        "load_convention": None,
        "unilateral": 0,
        "aliases": ("hanging leg raise",),
    },
)

# 已拍清单里核不上数据集来源的项：2026-09-09 拍定「哑铃分腿蹲」移出清单，故为空。
UNMATCHED_CATALOG_NAMES: tuple[str, ...] = ()

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _strip_line_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


# S1-03 种子维护只涉及 001–003；本助手固定只复制这三个文件（Stage 2 的 004 草稿表
# 与本组用例无关），此后由用例自行追加临时编号迁移。
_STAGE1_MIGRATION_FILES = (
    "001_stage0_runtime_and_settings.sql",
    "002_stage1_actions_profile.sql",
    "003_stage1_action_seed.sql",
)
# 动作目录的增量补充迁移（主要肌群）：目录读取列包含 ``muscle``，临时迁移目录必须带上它，
# 否则不是「只跑 Stage 1 迁移」的等价环境。临时目录要求编号从 1 连续，故按 004 复制。
_ACTION_MUSCLE_MIGRATION = (
    "013_stage4_action_muscle.sql",
    "004_action_muscle.sql",
)


def _migration_dir(tmp_path: Path) -> Path:
    """复制生产迁移（001–003）与目录增量迁移到临时目录；后续可再追加临时迁移。"""
    directory = tmp_path / "migrations"
    directory.mkdir(exist_ok=True)
    for name in _STAGE1_MIGRATION_FILES:
        shutil.copy(DEFAULT_MIGRATIONS_DIR / name, directory / name)
    source, target = _ACTION_MUSCLE_MIGRATION
    shutil.copy(DEFAULT_MIGRATIONS_DIR / source, directory / target)
    return directory


# 测试用临时迁移的种子 SQL：字面量书写，仅写入 tmp_path 的临时迁移目录。
DUPLICATE_SEED_SQL = (
    "INSERT INTO exercises (id, standard_name_zh, equipment_variant, record_type,"
    " load_convention, unilateral, recommendable, active, aliases_json, modes_json,"
    " source_ref, attribution, instructions_zh) VALUES ("
    " 'barbell-back-squat', '杠铃背蹲', 'barbell', 'reps_weight',"
    " 'barbell_includes_bar_total', 0, 0, 1, '[\"barbell full squat\"]', '[\"深蹲\"]',"
    " 'test-fixture', 'test fixture', '测试用虚构动作说明');"
)
EXTRA_CATALOG_ENTRY_SQL = (
    "INSERT INTO exercises (id, standard_name_zh, equipment_variant, record_type,"
    " load_convention, unilateral, recommendable, active, aliases_json, modes_json,"
    " source_ref, attribution, instructions_zh) VALUES ("
    " 'test-only-exercise', '测试虚构动作（非产品种子）', 'barbell', 'reps_weight',"
    " 'barbell_includes_bar_total', 0, 0, 1, '[\"test only exercise\"]', '[\"深蹲\"]',"
    " 'test-fixture', 'test fixture', '测试用虚构动作说明');"
)


async def _exercise_count(db) -> int:
    async def op(conn):
        async with conn.execute("SELECT COUNT(*) FROM exercises") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _profile_row(db) -> tuple[object, object, object]:
    async def op(conn):
        async with conn.execute(
            "SELECT id, profile_json, context_version FROM user_profile"
        ) as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 1
        return (rows[0]["id"], rows[0]["profile_json"], rows[0]["context_version"])

    return await db.under_lock(op)


async def _stop_exercise(db, exercise_id: str) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE exercises SET active = 0 WHERE id = ?", (exercise_id,)
        )


# ---------- 003 迁移边界 ----------


def test_seed_migration_is_insert_only_and_never_touches_profile() -> None:
    """种子只 INSERT：不建表、不改结构、不写用户事实（stage1.md §5 S1-03 边界）。"""
    sql = (DEFAULT_MIGRATIONS_DIR / SEED_MIGRATION_FILE).read_text(encoding="utf-8")
    body = _strip_line_comments(sql).strip()
    assert body.startswith("INSERT INTO exercises")
    assert body.count(";") == 1
    for keyword in ("UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "PRAGMA"):
        assert keyword not in body
    assert "user_profile" not in body


async def test_fresh_database_seeds_catalog_without_profile_facts(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        assert await db.migrate() == len(load_migrations())
        assert await _exercise_count(db) == SEEDED_EXERCISE_COUNT
        assert await _profile_row(db) == USER_PROFILE_ROW


# ---------- 逐项来源核对表 ----------


async def test_seed_rows_match_checked_product_table(tmp_path: Path) -> None:
    """逐项核对：身份、来源 ID、器械变式、记录口径、负重口径、单侧、别名。"""
    async with open_database(tmp_path / "app.db") as db:
        service = ActionCatalogService(db)
        for expected in SEED_ROWS:
            exercise = await service.get_by_id(str(expected["id"]))
            assert exercise is not None, f"缺少种子行：{expected['id']}"
            assert exercise.standard_name_zh == expected["standard_name_zh"]
            assert exercise.source_ref == f"exercises-dataset:{expected['source_id']}"
            assert exercise.equipment_variant == expected["equipment_variant"]
            assert exercise.record_type == expected["record_type"]
            assert exercise.load_convention == expected["load_convention"]
            assert exercise.unilateral is bool(expected["unilateral"])
            assert exercise.aliases == tuple(cast(tuple[str, ...], expected["aliases"]))
            assert exercise.attribution == ATTRIBUTION
            assert exercise.instructions_zh  # 文字数据随种子保留
            assert str(expected["source_name"]) in exercise.aliases


async def test_seed_covers_decided_catalog_except_unmatched_item(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        # 系统迁移（007）已将 Stage 1 已核对的 24 项置为可推荐（S3-02/D2 A）：
        # 推荐候选恰为核对清单本身，不多置一项、不新增目录行。
        catalog = await ActionCatalogService(db).recommendation_candidates()
        assert {exercise.id for exercise in catalog} == {
            str(row["id"]) for row in SEED_ROWS
        }
        assert len(catalog) == SEEDED_EXERCISE_COUNT
        exercises = await ExerciseRepo(db).list_all()
        names = {exercise.standard_name_zh for exercise in exercises}
        assert names == set(rules.CATALOG_STANDARD_NAMES) - set(UNMATCHED_CATALOG_NAMES)


async def test_seed_modes_and_flags_follow_decided_contract(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        exercises = await ExerciseRepo(db).list_all()
        assert len(exercises) == SEEDED_EXERCISE_COUNT
        for exercise in exercises:
            rules.validate_modes(exercise.modes)
            assert exercise.modes == rules.modes_for(exercise.standard_name_zh)
            assert exercise.record_type in rules.RECORD_TYPES
            if exercise.record_type == "reps_weight":
                assert exercise.load_convention in rules.LOAD_CONVENTIONS
            else:
                assert exercise.load_convention is None
            assert exercise.unilateral is rules.is_unilateral(exercise.standard_name_zh)
            # 003 种子写入 recommendable=0；007 系统迁移将已核对 24 项置 1（S3-02/D2 A）
            assert exercise.recommendable is True
            assert exercise.active is True
            assert exercise.source_ref.startswith("exercises-dataset:")
            for alias in exercise.aliases:
                assert not _CJK.search(alias)  # 中文口语别名不入库


# ---------- 编号迁移增量维护与重复导入 ----------


async def test_reimporting_seed_via_later_migration_is_rejected_atomically(
    tmp_path: Path,
) -> None:
    """重复导入不制造重复身份、不覆盖已停用状态：冲突迁移整体回滚。"""
    path = tmp_path / "app.db"
    directory = _migration_dir(tmp_path)
    async with open_database(path, migrations_dir=directory) as db:
        assert await db.migrate() == 4
        await _stop_exercise(db, "barbell-back-squat")

    (directory / "005_duplicate_seed.sql").write_text(
        DUPLICATE_SEED_SQL, encoding="utf-8"
    )
    async with open_database(path, migrate=False, migrations_dir=directory) as db:
        with pytest.raises(MigrationError, match="005_duplicate_seed"):
            await db.migrate()
        assert await db.pragma_value("user_version") == 4
        assert await _exercise_count(db) == SEEDED_EXERCISE_COUNT
        stopped = await ActionCatalogService(db).get_by_id("barbell-back-squat")
        assert stopped is not None and stopped.active is False
        assert await _profile_row(db) == USER_PROFILE_ROW


async def test_later_numbered_migration_can_extend_seed_incrementally(
    tmp_path: Path,
) -> None:
    """种子变更采用编号迁移增量维护：新增行不动已停用状态与既有行。"""
    path = tmp_path / "app.db"
    directory = _migration_dir(tmp_path)
    async with open_database(path, migrations_dir=directory) as db:
        assert await db.migrate() == 4
        await _stop_exercise(db, "hanging-leg-raise")

    (directory / "005_extra_catalog_entry.sql").write_text(
        EXTRA_CATALOG_ENTRY_SQL, encoding="utf-8"
    )
    async with open_database(path, migrations_dir=directory) as db:
        assert await db.migrate() == 5
        assert await _exercise_count(db) == SEEDED_EXERCISE_COUNT + 1
        added = await ActionCatalogService(db).get_by_id("test-only-exercise")
        assert added is not None and added.recommendable is False
        stopped = await ActionCatalogService(db).get_by_id("hanging-leg-raise")
        assert stopped is not None and stopped.active is False


async def test_seed_import_keeps_context_version_untouched(tmp_path: Path) -> None:
    """种子导入与系统迁移不推进统一业务版本，也不建立独立计数器（S1-02/S1-03 边界）。"""
    async with open_database(tmp_path / "app.db") as db:
        # 用生产迁移全量版本（含后续阶段新增编号迁移）：种子导入不建立用户事实。
        assert await db.migrate() == len(load_migrations())
        assert await _profile_row(db) == USER_PROFILE_ROW
        service = ActionCatalogService(db)
        assert await service.resolve("barbell full squat")
        # 007 系统迁移置已核对 24 项为可推荐，但不动正式档案与 context_version
        assert len(await service.recommendation_candidates()) == SEEDED_EXERCISE_COUNT
        assert await _profile_row(db) == USER_PROFILE_ROW


async def test_seed_aliases_are_valid_json_arrays(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:

        async def op(conn):
            async with conn.execute(
                "SELECT id, aliases_json, modes_json FROM exercises"
            ) as cursor:
                return await cursor.fetchall()

        rows = await db.under_lock(op)
        assert len(rows) == SEEDED_EXERCISE_COUNT
        for row in rows:
            aliases = json.loads(row["aliases_json"])
            modes = json.loads(row["modes_json"])
            assert isinstance(aliases, list) and aliases
            assert isinstance(modes, list) and modes
            assert json.loads(json.dumps(aliases, ensure_ascii=False)) == aliases


async def test_duplicate_standard_name_insert_is_rejected_by_catalog(
    tmp_path: Path,
) -> None:
    """一个中文标准名一个身份：第二个身份不得挂同一标准名。"""
    async with open_database(tmp_path / "app.db") as db, db.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO exercises (id, standard_name_zh, equipment_variant,"
                " record_type, load_convention, unilateral, recommendable, active,"
                " aliases_json, modes_json, source_ref, attribution, instructions_zh)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "test-only-duplicate-name",
                    "杠铃背蹲",
                    "barbell",
                    "reps_weight",
                    "barbell_includes_bar_total",
                    0,
                    0,
                    1,
                    '["test fixture"]',
                    '["深蹲"]',
                    "test-fixture",
                    "test fixture",
                    "测试用虚构动作说明",
                ),
            )
