# 内存数据集仓储的集成测试：对已入库的真实语料 exercises.zh-en.json 直接检索，验证文本匹配、
# facet 过滤（含折叠长名的等价召回）、命中排序与 limit、详情按身份读取与缺失返回 None，以及词表
# 对数据集实际取值的完整覆盖（词表漂移会在此失败）。不 mock 仓储、不伪造行。

from app.application.agent.harness.tools.exercise_dataset.store import (
    DEFAULT_DATASET_PATH,
    InMemoryExerciseDataset,
)
from app.application.agent.harness.tools.exercise_dataset.vocab import (
    BODY_PART_VALUES,
    EQUIPMENT_VALUES,
    MUSCLE_GROUP_ALIASES,
    MUSCLE_GROUP_VALUES,
    TARGET_VALUES,
    DatasetExercise,
)

CHEST_TOTAL = 163
LATS_TOTAL = 3  # 1 个规范码 lats + 2 个折叠长名 latissimus dorsi
SQUAT_TEXT = "squat"
SQUAT_LIMIT = 5


def _dataset() -> InMemoryExerciseDataset:
    return InMemoryExerciseDataset()


def _all_rows() -> tuple[DatasetExercise, ...]:
    return _dataset()._rows


async def test_default_path_points_at_the_committed_corpus() -> None:
    """默认路径就是仓库里已提交的数据集，规模稳定。"""
    assert DEFAULT_DATASET_PATH.is_file()
    assert len(_all_rows()) == 1324


async def test_text_search_matches_english_names_in_id_order() -> None:
    """文本检索：命中行都含该子串，按 id 升序，limit 截断。"""
    hits = await _dataset().search(text=SQUAT_TEXT, limit=SQUAT_LIMIT)

    assert len(hits) == SQUAT_LIMIT
    assert SQUAT_TEXT in " ".join(row.name for row in hits).lower()
    assert [row.id for row in hits] == sorted(row.id for row in hits)


async def test_facet_filter_accepts_canonical_value_and_bounds_results() -> None:
    """facet 过滤按规范英文命中，返回行的该 facet 全部等于过滤值。"""
    hits = await _dataset().search(body_part="chest", limit=1000)

    assert len(hits) == CHEST_TOTAL
    assert {row.body_part for row in hits} == {"chest"}


async def test_muscle_group_filter_recalls_folded_long_names() -> None:
    """规范码过滤把折叠的长名一并召回：lats 命中 lats 与 latissimus dorsi 两种原始取值。"""
    hits = await _dataset().search(muscle_group="lats", limit=1000)

    assert len(hits) == LATS_TOTAL
    assert {row.muscle_group for row in hits} == {"lats", "latissimus dorsi"}


async def test_text_and_facet_combine_and_empty_result_is_empty() -> None:
    """文本与 facet 组合收窄命中；无解组合返回空 tuple。"""
    combined = await _dataset().search(text="squat", body_part="upper legs", limit=100)
    assert combined and all(
        SQUAT_TEXT in row.name.lower() and row.body_part == "upper legs"
        for row in combined
    )

    empty = await _dataset().search(text="squat", body_part="chest", limit=100)
    assert empty == ()


async def test_get_detail_returns_full_bilingual_row_or_none() -> None:
    """按身份取详情给完整双语行；未知身份返回 None。"""
    row = await _dataset().get_detail("0001")

    assert row is not None
    assert row.name == "3/4 sit-up"
    assert row.instructions_zh and row.instructions_en
    assert len(row.steps_zh) == len(row.steps_en) >= 1

    assert await _dataset().get_detail("999999") is None


async def test_facet_vocabularies_cover_every_dataset_value() -> None:
    """词表覆盖数据集全部实际取值：简单 facet 精确相等，muscle_group 允许折叠长名。"""
    rows = _all_rows()

    assert {row.body_part for row in rows} == set(BODY_PART_VALUES)
    assert {row.equipment for row in rows} == set(EQUIPMENT_VALUES)
    assert {row.target for row in rows} == set(TARGET_VALUES)

    muscle_values = {row.muscle_group for row in rows}
    assert muscle_values <= (MUSCLE_GROUP_VALUES | set(MUSCLE_GROUP_ALIASES))
    assert MUSCLE_GROUP_VALUES <= muscle_values | set(MUSCLE_GROUP_ALIASES)
