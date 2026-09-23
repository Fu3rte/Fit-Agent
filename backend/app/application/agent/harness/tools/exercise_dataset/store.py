# 动作数据集的读取端口与内存实现：一次性读入已入库的静态语料 exercises.zh-en.json，按文本与归一后的
# facet 做确定性检索。数据集只有约 1300 行，无需建表与迁移。ExerciseDataset 是与工具同目录的可注入
# seam（对应 pi 的 ReadOperations）：默认用内存实现，将来换成基于表的实现只需另提供一个满足该协议的
# 对象并由组合根注入，工具与词表都不动。
# canonical 视图在这一层把 exercises 目录行与数据集行按 source_ref 联表：映射规则集中在此，工具不解析
# source_ref、不读 JSON、不建第二套规则。

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from app.application.agent.harness.tools.exercise_dataset.vocab import (
    MOVEMENT_PATTERN_ZH,
    DatasetExercise,
    equivalent_values,
    facet_label,
    resolve_facet,
)
from app.domain.actions.schema import Exercise, LoadConvention, RecordType

#: 组合根默认读取的数据集路径：backend/data 下的已提交语料。
DEFAULT_DATASET_PATH = (
    Path(__file__).resolve().parents[6] / "data" / "exercises.zh-en.json"
)

#: 数据集 manifest：catalog_revision、dataset_revision 与 canonical-only enrichment。
DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[6] / "data" / "exercises.zh-en.manifest.json"
)

_LEGACY_PREFIX = "exercises-dataset:"
_VERSIONED_PREFIX = "exercises-dataset@"

#: canonical ``Exercise.modes`` 的中文标签 → 规范英文。
_MOVEMENT_PATTERN_EN: Mapping[str, str] = {
    zh: en for en, zh in MOVEMENT_PATTERN_ZH.items()
}


class ExerciseCatalogUnavailable(RuntimeError):
    """canonical 动作目录、数据集映射或事实 revision 不可用：当前 Run 内无法核对目录。"""


@dataclass(frozen=True, slots=True)
class CanonicalExerciseHit:
    """canonical 目录行与数据集行的联表结果：身份与记录口径来自 canonical，中英 facet 与指导语来自数据集。"""

    exercise_id: str
    standard_name_zh: str
    name_en: str
    aliases: tuple[str, ...]
    equipment: str
    equipment_zh: str
    muscle_groups: tuple[str, ...]
    muscle_groups_zh: tuple[str, ...]
    movement_patterns: tuple[str, ...]
    record_type: RecordType
    load_convention: LoadConvention | None
    recommendable: bool
    instructions_zh: str | None
    instructions_en: str | None


class ExerciseDataset(Protocol):
    """动作数据集只读检索端口：按归一后的规范英文 facet 与文本召回，按身份取详情。"""

    async def search(
        self,
        *,
        text: str | None = None,
        body_part: str | None = None,
        equipment: str | None = None,
        target: str | None = None,
        muscle_group: str | None = None,
        limit: int,
    ) -> tuple[DatasetExercise, ...]:
        """按文本与规范英文 facet 检索；命中上限 limit，顺序确定。"""
        ...

    async def get_detail(self, exercise_id: str) -> DatasetExercise | None:
        """按数据集身份（数字串）取一行；不存在即 None。"""


class CanonicalExerciseDataset(ExerciseDataset, Protocol):
    """canonical 动作目录检索端口：在数据集能力之上按 canonical facet 联表检索并稳定截断。"""

    async def search_canonical(
        self,
        *,
        query: str | None,
        muscle_groups: Sequence[str] = (),
        equipment: Sequence[str] = (),
        movement_patterns: Sequence[str] = (),
        limit: int,
    ) -> tuple[CanonicalExerciseHit, ...]:
        """按 canonical 身份与归一后的规范英文 facet 检索；已按 -recommendable、标准名、身份排序并截断。"""
        ...


class _Manifest(BaseModel):
    """manifest 骨架：canonical 目录 revision、数据集语料 revision 与 canonical-only enrichment。"""

    model_config = ConfigDict(extra="forbid")

    catalog_revision: int
    dataset_revision: str
    canonical_only: Mapping[str, Mapping[str, Any]] = {}


class InMemoryExerciseDataset:
    """构造时读入语料、常驻内存的动作数据集检索实现。"""

    def __init__(self) -> None:
        raw = json.loads(DEFAULT_DATASET_PATH.read_text(encoding="utf-8"))
        self._rows: tuple[DatasetExercise, ...] = tuple(
            DatasetExercise.from_row(record) for record in raw
        )
        self._by_id = {row.id: row for row in self._rows}

    async def search(
        self,
        *,
        text: str | None = None,
        body_part: str | None = None,
        equipment: str | None = None,
        target: str | None = None,
        muscle_group: str | None = None,
        limit: int,
    ) -> tuple[DatasetExercise, ...]:
        """按文本与规范英文 facet 过滤，命中按数据集身份升序，取前 limit 行；折叠的长名一并召回。"""
        active = {
            field: equivalent_values(value)
            for field, value in (
                ("body_part", body_part),
                ("equipment", equipment),
                ("target", target),
                ("muscle_group", muscle_group),
            )
            if value is not None
        }
        matches = [
            row
            for row in self._rows
            if not (text and not row.matches_text(text))
            and all(getattr(row, field) in want for field, want in active.items())
        ]
        matches.sort(key=lambda row: row.id)
        return tuple(matches[:limit])

    async def get_detail(self, exercise_id: str) -> DatasetExercise | None:
        """按数据集身份取一行；不存在即 None。"""
        return self._by_id.get(exercise_id)


class InMemoryCanonicalExerciseDataset(InMemoryExerciseDataset):
    """canonical 目录 × 数据集语料的联表视图：构造时按 source_ref 全量校验并建好只读索引。"""

    def __init__(self, canonical: Sequence[Exercise], *, catalog_revision: int) -> None:
        if not DEFAULT_DATASET_PATH.is_file():
            raise ExerciseCatalogUnavailable(
                f"动作数据集缺失或不可读：{DEFAULT_DATASET_PATH}"
            )
        super().__init__()
        manifest = _load_manifest()
        if manifest.catalog_revision != catalog_revision:
            raise ExerciseCatalogUnavailable(
                "manifest catalog_revision="
                f"{manifest.catalog_revision} 与数据库 catalog_revision={catalog_revision} 不一致"
            )
        self._canonical = _join(canonical, manifest, self._by_id)

    async def search_canonical(
        self,
        *,
        query: str | None,
        muscle_groups: Sequence[str] = (),
        equipment: Sequence[str] = (),
        movement_patterns: Sequence[str] = (),
        limit: int,
    ) -> tuple[CanonicalExerciseHit, ...]:
        """跨 facet 取 AND、同 facet 取 OR，query 匹配中英名与别名；排序后取前 limit 行。"""
        needle = None if query is None else query.casefold()
        want_groups = set(muscle_groups)
        want_equipment = set(equipment)
        want_patterns = set(movement_patterns)
        matches = [
            hit
            for hit in self._canonical
            if (needle is None or needle in _haystack(hit))
            and (not want_groups or want_groups.intersection(hit.muscle_groups))
            and (not want_equipment or hit.equipment in want_equipment)
            and (not want_patterns or want_patterns.intersection(hit.movement_patterns))
        ]
        matches.sort(
            key=lambda hit: (
                not hit.recommendable,
                hit.standard_name_zh,
                hit.exercise_id,
            )
        )
        return tuple(matches[:limit])


def _load_manifest() -> _Manifest:
    """读入并校验 manifest 骨架；文件缺失、不可读或字段不合规即抛目录不可用。"""
    if not DEFAULT_MANIFEST_PATH.is_file():
        raise ExerciseCatalogUnavailable(f"数据集 manifest 缺失：{DEFAULT_MANIFEST_PATH}")
    try:
        return _Manifest.model_validate_json(
            DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as error:
        raise ExerciseCatalogUnavailable(
            f"数据集 manifest 不可读或不合规：{DEFAULT_MANIFEST_PATH}"
        ) from error


def _join(
    canonical: Sequence[Exercise],
    manifest: _Manifest,
    dataset_by_id: Mapping[str, DatasetExercise],
) -> tuple[CanonicalExerciseHit, ...]:
    """canonical 目录 × 数据集行：逐行解析 source_ref、校验占用与完整性，产出联表视图。"""
    if not canonical:
        raise ExerciseCatalogUnavailable("canonical 动作目录为空：无法核对动作事实")
    _assert_canonical_only_entries(manifest, canonical)
    claimed: dict[str, str] = {}
    hits: list[CanonicalExerciseHit] = []
    for exercise in canonical:
        dataset_id = _dataset_id_of(exercise.source_ref, manifest)
        if dataset_id is None:
            hits.append(_canonical_only_hit(exercise, manifest))
            continue
        row = dataset_by_id.get(dataset_id)
        if row is None:
            raise ExerciseCatalogUnavailable(
                f"{exercise.id} 的 source_ref 引用了不存在的数据集 id：{dataset_id}"
            )
        if dataset_id in claimed:
            raise ExerciseCatalogUnavailable(
                f"数据集 id {dataset_id} 被多个 canonical 动作占用："
                f"{claimed[dataset_id]} 与 {exercise.id}"
            )
        claimed[dataset_id] = exercise.id
        hits.append(_dataset_hit(exercise, row))
    return tuple(hits)


def _dataset_id_of(source_ref: str, manifest: _Manifest) -> str | None:
    """source_ref → 数据集 id；versioned 引用校验 revision，非数据集引用返回 None。"""
    if source_ref.startswith(_VERSIONED_PREFIX):
        revision, separator, dataset_id = source_ref[len(_VERSIONED_PREFIX) :].partition(
            ":"
        )
        if revision != manifest.dataset_revision:
            raise ExerciseCatalogUnavailable(
                f"source_ref 的 revision 与 manifest 不一致：{source_ref}"
            )
        if not separator or not dataset_id:
            raise ExerciseCatalogUnavailable(f"source_ref 缺少数据集 id：{source_ref}")
        return dataset_id
    if source_ref.startswith(_LEGACY_PREFIX):
        dataset_id = source_ref[len(_LEGACY_PREFIX) :]
        if not dataset_id:
            raise ExerciseCatalogUnavailable(f"source_ref 缺少数据集 id：{source_ref}")
        return dataset_id
    return None


def _canonical_value(field: str, value: str) -> str:
    """facet 取值 → 规范英文；越界即抛目录不可用。"""
    canonical = resolve_facet(field, value)
    if canonical is None:
        raise ExerciseCatalogUnavailable(f"{field} 取值不在词表内：{value!r}")
    return canonical


def _movement_patterns(exercise: Exercise) -> tuple[str, ...]:
    """canonical modes → 规范英文动作模式；未知 mode 即抛目录不可用。"""
    patterns: list[str] = []
    for mode in exercise.modes:
        pattern = _MOVEMENT_PATTERN_EN.get(mode)
        if pattern is None:
            raise ExerciseCatalogUnavailable(
                f"{exercise.id} 的动作模式不在词表内：{mode!r}"
            )
        patterns.append(pattern)
    return tuple(patterns)


def _groups(values: Sequence[str]) -> tuple[str, ...]:
    """肌群取值 → 规范英文，按输入顺序去重（主肌群保持首位）。"""
    return tuple(
        dict.fromkeys(_canonical_value("muscle_group", value) for value in values)
    )


def _hit(
    exercise: Exercise,
    *,
    name_en: str,
    equipment: str,
    groups: tuple[str, ...],
    instructions_zh: str | None,
    instructions_en: str | None,
) -> CanonicalExerciseHit:
    """canonical 行与数据集事实的联表投影。"""
    return CanonicalExerciseHit(
        exercise_id=exercise.id,
        standard_name_zh=exercise.standard_name_zh,
        name_en=name_en,
        aliases=exercise.aliases,
        equipment=equipment,
        equipment_zh=facet_label("equipment", equipment),
        muscle_groups=groups,
        muscle_groups_zh=tuple(
            facet_label("muscle_group", group) for group in groups
        ),
        movement_patterns=_movement_patterns(exercise),
        record_type=exercise.record_type,
        load_convention=exercise.load_convention,
        recommendable=exercise.recommendable,
        instructions_zh=instructions_zh,
        instructions_en=instructions_en,
    )


def _dataset_hit(exercise: Exercise, row: DatasetExercise) -> CanonicalExerciseHit:
    """数据集行联表投影：主肌群首位，secondary muscles 按数据集顺序归一去重。"""
    return _hit(
        exercise,
        name_en=row.name,
        equipment=row.equipment,
        groups=_groups((row.muscle_group, *row.secondary_muscles)),
        instructions_zh=row.instructions_zh,
        instructions_en=row.instructions_en,
    )


def _canonical_only_hit(
    exercise: Exercise, manifest: _Manifest
) -> CanonicalExerciseHit:
    """无数据集映射的 canonical 动作：identity 由 manifest 的 canonical-only enrichment 补齐。"""
    extra = manifest.canonical_only.get(exercise.id)
    if extra is None or extra.get("source_ref") != exercise.source_ref:
        raise ExerciseCatalogUnavailable(
            f"canonical 动作既无数据集映射也无 canonical-only 登记：{exercise.id}"
        )
    return _hit(
        exercise,
        name_en=str(extra["name_en"]),
        equipment=_canonical_value("equipment", str(extra["equipment"])),
        groups=_groups(tuple(str(group) for group in extra["muscle_groups"])),
        instructions_zh=None,
        instructions_en=None,
    )


def _assert_canonical_only_entries(
    manifest: _Manifest, canonical: Sequence[Exercise]
) -> None:
    """canonical-only 登记必须字段齐备，且指向真实、未被 dataset 映射的 canonical 动作。"""
    by_id = {exercise.id: exercise for exercise in canonical}
    for exercise_id, extra in manifest.canonical_only.items():
        for key in ("source_ref", "name_en", "equipment", "muscle_groups"):
            if key not in extra:
                raise ExerciseCatalogUnavailable(
                    f"canonical-only 登记缺少字段 {key}：{exercise_id}"
                )
        exercise = by_id.get(exercise_id)
        if exercise is None:
            raise ExerciseCatalogUnavailable(
                f"canonical-only 登记引用了不存在的 canonical 动作：{exercise_id}"
            )
        if extra["source_ref"] != exercise.source_ref:
            raise ExerciseCatalogUnavailable(
                f"canonical-only 登记的 source_ref 与目录不一致：{exercise_id}"
            )
        if _dataset_id_of(exercise.source_ref, manifest) is not None:
            raise ExerciseCatalogUnavailable(
                f"canonical-only 登记的动作已有 dataset 映射：{exercise_id}"
            )


def _haystack(hit: CanonicalExerciseHit) -> str:
    """canonical 标准名、别名与数据集英文名的检索文本。"""
    return " ".join((hit.standard_name_zh, hit.name_en, *hit.aliases)).casefold()
