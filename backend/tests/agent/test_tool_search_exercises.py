# search_exercises：canonical 目录检索工具。参数校验（query 长度、facet 数量、limit 边界、中英归一与
# 词表外拒绝）、查询语义（跨 facet AND、同 facet OR、中文名／别名／英文名匹配）、输出投影（canonical id、
# 稳定排序、recommendable 标志、合法空结果）与 failed 语义（store 的 ExerciseCatalogUnavailable 原样上抛）。
# 事实用 tmp_path 下的真实迁移库与真实 canonical 目录 ＋ 已入库真实语料；模型入口只用不可调用的替身。

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.harness.tools.common import TrainingHarnessContext
from app.application.agent.harness.tools.exercise_dataset.store import (
    ExerciseCatalogUnavailable,
    InMemoryCanonicalExerciseDataset,
)
from app.application.agent.harness.tools.exercise_dataset.tools import (
    SearchExercisesArgs,
    search_exercises,
)
from app.application.ports import ModelGateway
from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.infrastructure.database.connection import Database

BUSINESS_DAY = date(2026, 6, 1)

BARBELL_BACK_SQUAT = "barbell-back-squat"
BARBELL_BENCH_PRESS = "barbell-bench-press"
BARBELL_HACK_SQUAT = "barbell-hack-squat"
PLANK = "plank"

HIT_FIELDS = {
    "exercise_id",
    "standard_name_zh",
    "name_en",
    "aliases",
    "equipment",
    "equipment_zh",
    "muscle_groups",
    "muscle_groups_zh",
    "movement_patterns",
    "record_type",
    "load_convention",
    "recommendable",
    "instructions",
}


async def _unavailable_model(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("search_exercises 只读，不触达模型入口")


_MODEL = ModelGateway(
    text=_unavailable_model,
    structured=_unavailable_model,
    tools=_unavailable_model,
)


@dataclass(frozen=True, slots=True)
class _UnavailableDataset:
    """目录不可用的替身端口：只验证工具原样上抛 ExerciseCatalogUnavailable。"""

    async def search_canonical(self, **_kwargs: Any) -> Any:
        raise ExerciseCatalogUnavailable("canonical 目录不可用")

    async def search(self, **_kwargs: Any) -> Any:
        raise AssertionError("内部能力不在本测试路径")

    async def get_detail(self, **_kwargs: Any) -> Any:
        raise AssertionError("内部能力不在本测试路径")


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[TrainingHarnessContext]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        repositories = build_repositories(db)
        services = build_services(
            repositories, db, SqliteHealthProbe(db, db.path.parent)
        )
        catalog_revision = await db.pragma_value("user_version")
        if not isinstance(catalog_revision, int):
            raise RuntimeError(f"迁移后 user_version 不是整数：{catalog_revision!r}")
        dataset = InMemoryCanonicalExerciseDataset(
            await repositories.exercises.list_all(), catalog_revision=catalog_revision
        )
        yield TrainingHarnessContext(
            model=_MODEL,
            budget=ModelRequestBudget(),
            business_day=BUSINESS_DAY,
            profiles=repositories.profiles,
            plans=repositories.plans,
            catalog=repositories.exercises,
            records=services.records,
            stats=services.stats,
            dataset=dataset,
        )
    finally:
        await db.close()


async def _call(context: TrainingHarnessContext, args: Mapping[str, Any]) -> Any:
    content = await search_exercises.ainvoke(
        {"runtime": SimpleNamespace(context=context), **args}
    )
    return json.loads(content)


def test_args_require_at_least_one_filter() -> None:
    """query 与三个 facet 全空即参数错误；给出任一条件即通过。"""
    with pytest.raises(ValidationError, match="至少提供一个动作检索条件"):
        SearchExercisesArgs(runtime=None)
    SearchExercisesArgs(runtime=None, query="深蹲")
    SearchExercisesArgs(runtime=None, muscle_groups=("chest",))
    SearchExercisesArgs(runtime=None, equipment=("barbell",))
    SearchExercisesArgs(runtime=None, movement_patterns=("squat",))


def test_query_length_facet_count_and_limit_bounds() -> None:
    """query 1..100、facet 至多 8 项、limit 1..25 是 Schema 边界，越界在参数校验阶段拒绝。"""
    SearchExercisesArgs(runtime=None, query="深").model_dump()
    SearchExercisesArgs(runtime=None, query="深" * 100).model_dump()
    for bad_query in ("", "深" * 101):
        with pytest.raises(ValidationError):
            SearchExercisesArgs(runtime=None, query=bad_query)

    eight = (
        "chest",
        "lats",
        "abdominals",
        "glutes",
        "quadriceps",
        "hamstrings",
        "biceps",
        "triceps",
    )
    assert SearchExercisesArgs(runtime=None, muscle_groups=eight).muscle_groups == eight
    with pytest.raises(ValidationError):
        SearchExercisesArgs(runtime=None, muscle_groups=(*eight, "calves"))

    for limit in (0, 26):
        with pytest.raises(ValidationError):
            SearchExercisesArgs(runtime=None, query="深蹲", limit=limit)
    for limit in (1, 25):
        assert SearchExercisesArgs(runtime=None, query="深蹲", limit=limit).limit == limit


def test_facets_accept_chinese_and_english_and_normalize() -> None:
    """中文、规范英文与大小写都折成规范英文；facet 词表外的取值在参数校验阶段拒绝。"""
    args = SearchExercisesArgs(
        runtime=None,
        muscle_groups=("胸部", "Lats"),
        equipment=("杠铃", "body weight"),
        movement_patterns=("深蹲", "Hip Hinge"),
    )
    assert args.muscle_groups == ("chest", "lats")
    assert args.equipment == ("barbell", "body weight")
    assert args.movement_patterns == ("squat", "hip hinge")

    for field, vocab_field, value in (
        ("muscle_groups", "muscle_group", "不存在的肌群"),
        ("equipment", "equipment", "不存在的器械"),
        ("movement_patterns", "movement_pattern", "不存在的模式"),
    ):
        with pytest.raises(ValidationError) as exc:
            SearchExercisesArgs(runtime=None, **{field: (value,)})
        assert vocab_field in str(exc.value)
        assert "不在词表内" in str(exc.value)


def test_undeclared_argument_is_rejected() -> None:
    """未声明字段被 args_schema 拒绝。"""
    with pytest.raises(ValidationError):
        SearchExercisesArgs.model_validate({"runtime": None, "query": "深蹲", "offset": 1})


async def test_query_matches_chinese_name_alias_and_english_name(tmp_path: Path) -> None:
    """query 同时命中中文标准名、canonical 别名与数据集英文名。"""
    async with _harness(tmp_path) as context:
        by_name = await _call(context, {"query": "杠铃背蹲"})
        by_alias = await _call(context, {"query": "杠铃深蹲"})
        by_english = await _call(context, {"query": "barbell bench press"})

    assert BARBELL_BACK_SQUAT in _ids(by_name)
    assert BARBELL_BACK_SQUAT in _ids(by_alias)
    assert _ids(by_english) == [BARBELL_BENCH_PRESS]


async def test_facets_filter_and_combine_with_and_or(tmp_path: Path) -> None:
    """同 facet 多值取 OR，跨 facet 取 AND；命中行的 facet 事实自洽。"""
    async with _harness(tmp_path) as context:
        either = await _call(
            context, {"equipment": ("barbell", "dumbbell"), "limit": 25}
        )
        both = await _call(
            context,
            {
                "equipment": ("barbell", "dumbbell"),
                "movement_patterns": ("hip hinge",),
                "limit": 25,
            },
        )
        chest_only = await _call(context, {"muscle_groups": ("胸部",), "limit": 25})

    exercises = either["exercises"]
    assert exercises
    assert all(hit["equipment"] in ("barbell", "dumbbell") for hit in exercises)
    assert all(
        hit["equipment"] in ("barbell", "dumbbell")
        and "hip hinge" in hit["movement_patterns"]
        for hit in both["exercises"]
    )
    assert both["returned"] <= either["returned"]
    assert all("chest" in hit["muscle_groups"] for hit in chest_only["exercises"])


async def test_output_projects_canonical_identity_and_stable_order(tmp_path: Path) -> None:
    """每项只有契约字段，身份是 canonical id，命中按 -recommendable、标准名、身份稳定排序。"""
    async with _harness(tmp_path) as context:
        payload = await _call(context, {"query": "卧推", "limit": 25})
        catalog = await context.catalog.list_all()

    exercises = payload["exercises"]
    assert set(payload) == {"exercises", "returned"}
    assert payload["returned"] == len(exercises)
    assert all(set(hit) == HIT_FIELDS for hit in exercises)

    canonical_ids = {exercise.id for exercise in catalog}
    assert set(_ids(payload)) <= canonical_ids

    order = [
        (not hit["recommendable"], hit["standard_name_zh"], hit["exercise_id"])
        for hit in exercises
    ]
    assert order == sorted(order)


async def test_limit_is_applied_after_sorting(tmp_path: Path) -> None:
    """limit 在排序之后截断：limit=1 只回排序后的第一条。"""
    async with _harness(tmp_path) as context:
        full = await _call(context, {"query": "卧推", "limit": 25})
        first = await _call(context, {"query": "卧推", "limit": 1})

    assert first["returned"] == 1
    assert _ids(first) == _ids(full)[:1]


async def test_recommendable_false_is_returned_with_its_flag(tmp_path: Path) -> None:
    """recommendable=false 的动作照常返回并保留标志。"""
    async with _harness(tmp_path) as context:
        payload = await _call(context, {"query": "杠铃哈克深蹲", "limit": 10})

    hit = _hit(payload["exercises"], BARBELL_HACK_SQUAT)
    assert hit["recommendable"] is False


async def test_legal_empty_result_is_empty(tmp_path: Path) -> None:
    """合法查询无命中：exercises 为空且 returned 为 0，不编造候选。"""
    async with _harness(tmp_path) as context:
        payload = await _call(context, {"query": "完全不存在的动作xyz", "limit": 25})

    assert payload == {"exercises": [], "returned": 0}


async def test_canonical_only_plank_uses_the_manifest_enrichment(tmp_path: Path) -> None:
    """无数据集映射的 canonical 动作按 manifest 的 canonical-only enrichment 投影，instructions 为 null。"""
    async with _harness(tmp_path) as context:
        payload = await _call(context, {"query": "平板支撑", "limit": 25})

    hit = _hit(payload["exercises"], PLANK)
    assert hit["name_en"] == "plank"
    assert hit["equipment"] == "body weight"
    assert hit["equipment_zh"] == "自重"
    assert hit["muscle_groups"] == ["core"]
    assert hit["muscle_groups_zh"] == ["核心"]
    assert hit["movement_patterns"] == ["core"]
    assert hit["instructions"] is None


async def test_store_catalog_unavailable_is_raised_unchanged(tmp_path: Path) -> None:
    """store 抛出的 ExerciseCatalogUnavailable 原样上抛，不被工具吞掉。"""
    async with _harness(tmp_path) as context:
        with pytest.raises(ExerciseCatalogUnavailable):
            await _call(replace(context, dataset=_UnavailableDataset()), {"query": "深蹲"})


def test_runtime_is_hidden_from_the_model_visible_schema() -> None:
    """模型可见 Schema 只含五个参数；注入的 runtime 不进模型可见 Schema。"""
    visible = cast(Any, search_exercises.tool_call_schema).model_json_schema()

    assert set(visible["properties"]) == {
        "query",
        "muscle_groups",
        "equipment",
        "movement_patterns",
        "limit",
    }
    assert "runtime" not in visible["properties"]
    assert search_exercises.args_schema is SearchExercisesArgs
    assert SearchExercisesArgs.model_json_schema()["additionalProperties"] is False


def _ids(payload: Mapping[str, Any]) -> list[str]:
    return [hit["exercise_id"] for hit in payload["exercises"]]


def _hit(hits: list[dict[str, Any]], exercise_id: str) -> dict[str, Any]:
    """按稳定身份取一条检索结果；缺失即失败，不静默降级。"""
    return next(hit for hit in hits if hit["exercise_id"] == exercise_id)
