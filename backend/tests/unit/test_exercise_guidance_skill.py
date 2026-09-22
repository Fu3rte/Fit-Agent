# ST-05 exercise-guidance Skill：真实 SkillLoader 校验正文与两个 reference，并把文档里的
# Tool 名、目录字段与动作 id 逐项对回当前代码与迁移后的真实动作目录。

import re
from pathlib import Path
from typing import Any

from app.application.agent.contracts import LoadedSkill
from app.application.agent.harness.tools.general import (
    GENERAL_INTENT_TOOLS,
    GENERAL_SKILL_NAMES,
)
from app.application.agent.harness.tools.training import (
    PLANNING_TOOLS,
    search_exercises,
)
from app.domain.actions.rules import LOAD_CONVENTIONS, RECORD_TYPES
from app.domain.records.schema import WorkoutSetInput
from app.infrastructure.skills.loader import SkillLoader
from config import skills_dir
from tests.agent.test_harness_training_tools import BUSINESS_DAY, _harness

SKILL_NAME = "exercise-guidance"
REFERENCE_PATHS = ("references/guidance-rules.md", "references/few-shots.md")

#: 文档承诺的 ``search_exercises`` 输出字段，与 Tool 实际载荷逐键对齐。
CATALOG_FIELDS = (
    "exercise_id",
    "standard_name_zh",
    "aliases",
    "equipment_variant",
    "record_type",
    "load_convention",
    "recommendable",
    "min_load_increment_kg",
    "starting_load",
)

BARBELL_BENCH_PRESS = "barbell-bench-press"
PULL_UP = "pull-up"

#: 文档点名的动作事实：``exercise_id`` →（标准名、器械、记录口径、负重口径、加重单位）。
DOCUMENTED_EXERCISES: dict[str, tuple[str, str, str, str | None, float | None]] = {
    "barbell-bench-press": (
        "杠铃平板卧推",
        "barbell",
        "reps_weight",
        "barbell_includes_bar_total",
        2.5,
    ),
    "barbell-close-grip-bench-press": (
        "杠铃窄距卧推",
        "barbell",
        "reps_weight",
        "barbell_includes_bar_total",
        2.5,
    ),
    "barbell-decline-bench-press": (
        "杠铃下斜卧推",
        "barbell",
        "reps_weight",
        "barbell_includes_bar_total",
        2.5,
    ),
    "barbell-incline-bench-press": (
        "杠铃上斜卧推",
        "barbell",
        "reps_weight",
        "barbell_includes_bar_total",
        2.5,
    ),
    "dumbbell-bench-press": (
        "哑铃平板卧推",
        "dumbbell",
        "reps_weight",
        "dumbbell_per_hand",
        2.5,
    ),
    "dumbbell-incline-bench-press": (
        "哑铃上斜卧推",
        "dumbbell",
        "reps_weight",
        "dumbbell_per_hand",
        2.5,
    ),
    "chin-up": ("自重反手引体向上", "bodyweight", "reps_bodyweight", None, None),
    "pull-up": ("自重引体向上", "bodyweight", "reps_bodyweight", None, None),
    "weighted-pull-up": (
        "负重引体",
        "weighted",
        "reps_weight",
        "external_added_weight",
        5.0,
    ),
    "barbell-seated-overhead-press": (
        "杠铃坐姿肩推",
        "barbell",
        "reps_weight",
        "barbell_includes_bar_total",
        2.5,
    ),
    "seated-dumbbell-shoulder-press": (
        "坐姿哑铃肩推",
        "dumbbell",
        "reps_weight",
        "dumbbell_per_hand",
        2.5,
    ),
}

#: 文档写明的检索结果：查询词 → 命中的 ``exercise_id`` 列表，顺序即 Tool 返回顺序。
DOCUMENTED_HITS: dict[str, list[str]] = {
    "卧推": [
        "barbell-bench-press",
        "barbell-close-grip-bench-press",
        "barbell-decline-bench-press",
        "barbell-incline-bench-press",
        "dumbbell-bench-press",
        "dumbbell-incline-bench-press",
    ],
    "引体": ["chin-up", "pull-up", "weighted-pull-up"],
    "推举": [
        "barbell-clean-and-press",
        "barbell-seated-overhead-press",
        "dumbbell-arnold-press",
        "dumbbell-standing-overhead-press",
        "seated-dumbbell-shoulder-press",
    ],
    "颈后推举": [],
    "barbell bench press": ["barbell-bench-press"],
}

#: 只取 kebab 形式的反引号片段：目录动作 id 的写法，排除文件名与路径。
_ID_PATTERN = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`")


def _load() -> LoadedSkill:
    """生产装配使用的同一份 Skill 根目录与同一个加载器。"""
    return SkillLoader(skills_dir()).load(SKILL_NAME)


def _skill_text(skill: LoadedSkill) -> str:
    return "\n".join((skill.body, *(ref.text for ref in skill.references)))


def _mounted_names() -> set[str]:
    return {tool.name for tool in GENERAL_INTENT_TOOLS["general"]}


def _unmounted_names() -> set[str]:
    """本 Intent 白名单之外、当前代码里存在的 Tool 名：指令不得点名它们。"""
    mounted = _mounted_names()
    every = {tool.name for tools in GENERAL_INTENT_TOOLS.values() for tool in tools}
    every |= {tool.name for tool in PLANNING_TOOLS}
    return every - mounted


def test_metadata_and_both_references_load() -> None:
    """frontmatter 与两个 reference 都被真实加载器读到，正文按 Markdown 链接指向它们。"""
    skill = _load()

    assert skill.metadata.name == SKILL_NAME
    assert skill.metadata.description
    assert tuple(ref.path for ref in skill.references) == REFERENCE_PATHS
    assert all(ref.text.strip() for ref in skill.references)
    assert all(f"({path})" in skill.body for path in REFERENCE_PATHS)


def test_general_skill_names_resolve_this_skill_through_the_real_loader() -> None:
    """General 装载矩阵里的该名称唯一，并且真实 SkillLoader 按同一个名称命中。"""
    loader = SkillLoader(skills_dir())

    assert GENERAL_SKILL_NAMES.count(SKILL_NAME) == 1
    assert loader.load(SKILL_NAME).metadata.name == SKILL_NAME


def test_skill_text_names_only_the_tools_mounted_for_general() -> None:
    """文档点名的 Tool 都在本 Intent 白名单内：目录事实只有 ``search_exercises`` 一个入口。"""
    text = _skill_text(_load())

    assert "search_exercises" in text
    assert not [name for name in _unmounted_names() if name in text]


def test_skill_text_names_the_current_record_and_load_vocabularies() -> None:
    """三类记录口径与六个负重口径取值都在当前领域词表内，取值域无扩写。"""
    text = _skill_text(_load())

    for record_type in RECORD_TYPES:
        assert f"`{record_type}`" in text
    for convention in LOAD_CONVENTIONS:
        assert f"`{convention}`" in text


async def test_documented_catalog_fields_and_ids_match_the_real_catalog(tmp_path: Path) -> None:
    """Tool 载荷字段、starting_load 两态与文档点名的动作事实全部对回真实目录。"""
    async with _harness(tmp_path, tools=(search_exercises,)) as h:
        payloads = {
            query: await h.payload("search_exercises", {"query": query})
            for query in DOCUMENTED_HITS
        }
        empty = payloads["颈后推举"]

        workout_id = await h.seed_workout(
            BUSINESS_DAY,
            [
                WorkoutSetInput(
                    exercise_id=BARBELL_BENCH_PRESS,
                    set_no=1,
                    set_type="work",
                    load_convention="barbell_includes_bar_total",
                    weight_kg=60.0,
                    reps=5,
                )
            ],
        )
        # 起始负荷在写入之后重取：known 引用的就是刚写入的那一组。
        known = _hit(
            await h.payload("search_exercises", {"query": "卧推"}), BARBELL_BENCH_PRESS
        )

        bodyweight = _hit(payloads["引体"], PULL_UP)
        english_alias = _hit(payloads["barbell bench press"], BARBELL_BENCH_PRESS)
        documented = {
            hit["exercise_id"]: hit
            for payload in payloads.values()
            for hit in payload
        }
        catalog_ids = {exercise.id for exercise in await h.context.catalog.list_all()}

    assert [hit["exercise_id"] for hit in payloads["卧推"]] == DOCUMENTED_HITS["卧推"]
    assert [hit["exercise_id"] for hit in payloads["引体"]] == DOCUMENTED_HITS["引体"]
    assert [hit["exercise_id"] for hit in payloads["推举"]] == DOCUMENTED_HITS["推举"]
    assert [hit["exercise_id"] for hit in payloads["barbell bench press"]] == DOCUMENTED_HITS[
        "barbell bench press"
    ]
    assert empty == []

    assert set(known) == set(CATALOG_FIELDS)
    assert known["starting_load"] == {
        "status": "known",
        "weight_kg": 60.0,
        "source_workout_session_id": workout_id,
        "source_set_no": 1,
    }
    assert bodyweight["record_type"] == "reps_bodyweight"
    assert bodyweight["load_convention"] is None
    assert bodyweight["min_load_increment_kg"] is None
    assert bodyweight["starting_load"] == {"status": "needs_calibration"}
    assert english_alias["aliases"] == ["卧推", "杠铃卧推", "barbell bench press"]

    for exercise_id, facts in DOCUMENTED_EXERCISES.items():
        hit = documented[exercise_id]
        assert (
            hit["standard_name_zh"],
            hit["equipment_variant"],
            hit["record_type"],
            hit["load_convention"],
            hit["min_load_increment_kg"],
        ) == facts

    text = _skill_text(_load())
    assert all(f"`{field}`" in text for field in CATALOG_FIELDS)
    documented_ids = set(_ID_PATTERN.findall(text))
    assert documented_ids <= catalog_ids
    assert set(DOCUMENTED_EXERCISES) <= documented_ids


def _hit(hits: list[dict[str, Any]], exercise_id: str) -> dict[str, Any]:
    """按稳定身份取一条检索结果；缺失即失败，不静默降级。"""
    return next(hit for hit in hits if hit["exercise_id"] == exercise_id)
