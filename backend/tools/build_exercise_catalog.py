"""离线生成动作目录迁移 ``006_expand_exercise_catalog.sql``。

输入（只读）：
- ``backend/tools/exercise_catalog.json``：受控目录源文件（110 条，人工维护）。
- ``/home/finnian/code/github-repos/exercises-dataset/data/exercises.json``：固定 commit 的原始数据集。

输出：
- ``backend/storage/migrations/006_expand_exercise_catalog.sql``（``--check`` 只校验并与已提交文件逐字节比较）。

只用 Python 标准库。任何校验失败立即非零退出，不产出半成品 SQL。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("exercise_catalog.json")
DATASET_PATH = Path(
    "/home/finnian/code/github-repos/exercises-dataset/data/exercises.json"
)
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "storage"
    / "migrations"
    / "006_expand_exercise_catalog.sql"
)

DATASET_COMMIT = "7455efae41b330c265e7cd4b78dfa848e7ce5ebd"
SOURCE_REF_PREFIX = f"exercises-dataset@{DATASET_COMMIT}"
NEW_ATTRIBUTION = "exercises-dataset (MIT, © 2026 Hasan Emir Yıldırım)"

TOTAL = 110
RECOMMENDABLE = 60
RECORD_ONLY = 50
NEW_TOTAL = 83

# 001 + 002 已落库的 27 行：只补 aliases，不重写行本身。
EXISTING_IDS = frozenset(
    {
        "barbell-back-squat",
        "barbell-deadlift",
        "barbell-romanian-deadlift",
        "leg-press-45",
        "bulgarian-split-squat",
        "barbell-bench-press",
        "dumbbell-bench-press",
        "dumbbell-incline-bench-press",
        "seated-dumbbell-shoulder-press",
        "dumbbell-lateral-raise",
        "dumbbell-reverse-fly",
        "barbell-bent-over-row",
        "seated-cable-row",
        "one-arm-dumbbell-row",
        "lat-pulldown",
        "pull-up",
        "seated-leg-curl",
        "leg-extension",
        "machine-standing-calf-raise",
        "dumbbell-biceps-curl",
        "cable-pushdown",
        "cable-overhead-triceps-extension",
        "parallel-bar-dip",
        "hanging-leg-raise",
        "weighted-pull-up",
        "plank",
        "front-lever",
    }
)

# 仅允许既有口径已有先例的 7 个器械族（dataset equipment → 目录 equipment_variant）。
EQUIPMENT_VARIANTS = {
    "barbell": "barbell",
    "dumbbell": "dumbbell",
    "cable": "cable",
    "body weight": "bodyweight",
    "leverage machine": "leverage_machine",
    "sled machine": "sled_machine",
    "weighted": "weighted",
}

# equipment_variant → 允许的（记录口径, 负重口径, 最小加重）三元组。
RECORD_SHAPES: dict[str, frozenset[tuple[str, str | None, float | None]]] = {
    "barbell": frozenset({("reps_weight", "barbell_includes_bar_total", 2.5)}),
    "dumbbell": frozenset({("reps_weight", "dumbbell_per_hand", 2.5)}),
    "cable": frozenset({("reps_weight", "machine_pin_displayed_value", 5.0)}),
    "leverage_machine": frozenset({("reps_weight", "machine_pin_displayed_value", 5.0)}),
    "sled_machine": frozenset({("reps_weight", "plate_loaded_total_excluding_empty", 2.5)}),
    "weighted": frozenset({("reps_weight", "external_added_weight", 5.0)}),
    "bodyweight": frozenset(
        {("reps_bodyweight", None, None), ("time", None, None)}
    ),
}

MODE_VOCABULARY = frozenset(
    {
        "深蹲",
        "髋铰链",
        "水平推",
        "垂直推",
        "水平拉",
        "垂直拉",
        "肩孤立",
        "膝屈",
        "膝伸",
        "小腿（踝跖屈）",
        "肘屈",
        "肘伸",
        "核心",
    }
)

FIELDS = (
    "id",
    "source_id",
    "standard_name_zh",
    "canonical_name_en",
    "aliases",
    "equipment_variant",
    "record_type",
    "load_convention",
    "min_load_increment_kg",
    "recommendable",
    "modes",
)

MIN_ALIASES = 2
MAX_ALIASES = 5

_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_INSERT_HEADER = (
    "INSERT INTO exercises (\n"
    "    id, standard_name_zh, equipment_variant, record_type, load_convention,\n"
    "    min_load_increment_kg, recommendable, modes_json, aliases_json, source_ref,"
    " attribution\n"
    ") VALUES\n"
)


class CatalogError(Exception):
    """目录或数据集校验不通过：立即非零退出，不生成 SQL。"""


def clean_en(name: str) -> str:
    """数据集英文名的已知错误拼写修复（45в° / peacher / revers）与空白规整。"""
    cleaned = name.replace("в°", "°")
    cleaned = re.sub(r"\bpeacher\b", "preacher", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\brevers\b", "reverse", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize(value: str) -> str:
    """别名规范化：Unicode NFKC + 去首尾空白 + casefold + 连续空白折叠为单空格。"""
    collapsed = re.sub(r"\s+", " ", value)
    return unicodedata.normalize("NFKC", collapsed.strip()).casefold()


def slug(name: str) -> str:
    """canonical 英文名 → kebab-case 稳定 id。"""
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _json_literal(values: list[str]) -> str:
    return _sql_literal(json.dumps(values, ensure_ascii=False))


def _load_catalog() -> list[dict[str, Any]]:
    try:
        rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CatalogError(f"目录源文件无法读取或不是合法 JSON：{error}") from error
    if not isinstance(rows, list):
        raise CatalogError("目录源文件顶层必须是数组")
    for row in rows:
        if not isinstance(row, dict) or tuple(row.keys()) != FIELDS:
            raise CatalogError(f"目录条目字段集不符（必须恰为 {FIELDS}）：{row!r}")
    return rows


def _load_dataset() -> dict[str, dict[str, Any]]:
    try:
        items = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CatalogError(f"数据集无法读取或不是合法 JSON：{error}") from error
    return {str(item["id"]): item for item in items}


def _check_alias_shape(row: dict[str, Any]) -> None:
    aliases = row["aliases"]
    if not isinstance(aliases, list):
        raise CatalogError(f"{row['id']}: aliases 必须是数组")
    if not MIN_ALIASES <= len(aliases) <= MAX_ALIASES:
        raise CatalogError(
            f"{row['id']}: aliases 必须为 {MIN_ALIASES}–{MAX_ALIASES} 个，实际 {len(aliases)}"
        )
    for alias in aliases:
        if not isinstance(alias, str):
            raise CatalogError(f"{row['id']}: alias 必须是非空字符串：{alias!r}")
        if alias == "" or alias.strip() != alias:
            raise CatalogError(f"{row['id']}: alias 不得为空或含首尾空白：{alias!r}")
        if normalize(alias) == "":
            raise CatalogError(f"{row['id']}: alias 规范化后为空：{alias!r}")
    if row["canonical_name_en"] not in aliases:
        raise CatalogError(f"{row['id']}: canonical_name_en 必须出现在 aliases 中")


def _check_modes(row: dict[str, Any]) -> None:
    modes = row["modes"]
    if not isinstance(modes, list) or not modes:
        raise CatalogError(f"{row['id']}: modes 不能为空")
    if len(set(modes)) != len(modes):
        raise CatalogError(f"{row['id']}: modes 有重复取值：{modes!r}")
    unknown = [mode for mode in modes if mode not in MODE_VOCABULARY]
    if unknown:
        raise CatalogError(f"{row['id']}: modes 超出 13 项词表：{unknown!r}")


def _check_record_shape(row: dict[str, Any]) -> None:
    shape = (row["record_type"], row["load_convention"], row["min_load_increment_kg"])
    if shape not in RECORD_SHAPES[row["equipment_variant"]]:
        raise CatalogError(
            f"{row['id']}: 记录口径/负重口径/增重单位与器械既有口径不符：{shape!r}"
        )


def _check_dataset_row(row: dict[str, Any], dataset: dict[str, dict[str, Any]]) -> None:
    source_id = row["source_id"]
    if source_id is None:
        if row["id"] != "plank":
            raise CatalogError(f"{row['id']}: 只有既有行 plank 允许没有 dataset source_id")
        return
    if not isinstance(source_id, str) or not re.fullmatch(r"[0-9]{4}", source_id):
        raise CatalogError(f"{row['id']}: source_id 必须是 4 位数字字符串：{source_id!r}")
    item = dataset.get(source_id)
    if item is None:
        raise CatalogError(f"{row['id']}: source_id {source_id} 在数据集中不存在")
    equipment = str(item["equipment"])
    if equipment not in EQUIPMENT_VARIANTS:
        raise CatalogError(f"{row['id']}: 数据集器械 {equipment!r} 不在允许的 7 个族内")
    if EQUIPMENT_VARIANTS[equipment] != row["equipment_variant"]:
        raise CatalogError(
            f"{row['id']}: equipment_variant {row['equipment_variant']!r} 与数据集"
            f" {equipment!r} 不一致"
        )
    canonical = clean_en(str(item["name"]))
    if normalize(row["canonical_name_en"]) != normalize(canonical):
        raise CatalogError(
            f"{row['id']}: canonical_name_en {row['canonical_name_en']!r} 与数据集"
            f" {canonical!r} 不一致"
        )


def _check_id(row: dict[str, Any]) -> None:
    if not _SLUG.fullmatch(row["id"]):
        raise CatalogError(f"id 不是 kebab-case：{row['id']!r}")
    if row["id"] in EXISTING_IDS:
        return
    base = slug(row["canonical_name_en"])
    if row["id"] not in {base, f"{base}-{row['source_id']}"}:
        raise CatalogError(
            f"{row['id']}: 新 id 必须是 canonical 英文名的 kebab-case，冲突时追加 source_id"
        )


def validate(rows: list[dict[str, Any]], dataset: dict[str, dict[str, Any]]) -> None:
    """§3 的全部校验项；任一不通过即抛 CatalogError。"""
    if len(rows) != TOTAL:
        raise CatalogError(f"目录必须恰好 {TOTAL} 条，实际 {len(rows)}")

    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise CatalogError(f"id 不唯一：{sorted({i for i in ids if ids.count(i) > 1})}")
    names = [row["standard_name_zh"] for row in rows]
    if len(set(names)) != len(names):
        raise CatalogError(
            f"standard_name_zh 不唯一：{sorted({n for n in names if names.count(n) > 1})}"
        )
    source_ids = [row["source_id"] for row in rows if row["source_id"] is not None]
    if len(set(source_ids)) != len(source_ids):
        raise CatalogError(
            f"source_id 不唯一：{sorted({s for s in source_ids if source_ids.count(s) > 1})}"
        )
    if EXISTING_IDS - set(ids):
        raise CatalogError(
            f"既有 27 行的 id 必须全部保留在目录中，缺失 {sorted(EXISTING_IDS - set(ids))}"
        )

    alias_owners: dict[str, str] = {}
    name_owner = {normalize(name): row["id"] for row, name in zip(rows, names)}
    for row in rows:
        _check_id(row)
        _check_alias_shape(row)
        _check_modes(row)
        if row["equipment_variant"] not in RECORD_SHAPES:
            raise CatalogError(f"{row['id']}: equipment_variant 不在 7 个族内")
        _check_record_shape(row)
        _check_dataset_row(row, dataset)
        if not isinstance(row["recommendable"], bool):
            raise CatalogError(f"{row['id']}: recommendable 必须是布尔值")
        if row["id"] in EXISTING_IDS and not row["recommendable"]:
            raise CatalogError(f"{row['id']}: 既有行的 recommendable 必须保持为 true")
        for alias in row["aliases"]:
            key = normalize(alias)
            owner = alias_owners.get(key)
            if owner is not None and owner != row["id"]:
                raise CatalogError(
                    f"alias {alias!r} 规范化后与 {owner!r} 冲突（{row['id']}）"
                )
            alias_owners[key] = row["id"]
            name_clash = name_owner.get(key)
            if name_clash is not None and name_clash != row["id"]:
                raise CatalogError(
                    f"alias {alias!r} 与其他动作标准名冲突（{row['id']} ↔ {name_clash}）"
                )

    recommendable = [row for row in rows if row["recommendable"]]
    if len(recommendable) != RECOMMENDABLE:
        raise CatalogError(
            f"recommendable=true 必须恰好 {RECOMMENDABLE} 条，实际 {len(recommendable)}"
        )
    record_only = len(rows) - len(recommendable)
    if record_only != RECORD_ONLY:
        raise CatalogError(f"recommendable=false 必须恰好 {RECORD_ONLY} 条，实际 {record_only}")
    new_rows = [row for row in rows if row["id"] not in EXISTING_IDS]
    if len(new_rows) != NEW_TOTAL:
        raise CatalogError(f"新增动作必须恰好 {NEW_TOTAL} 条，实际 {len(new_rows)}")


def build_sql(rows: list[dict[str, Any]]) -> str:
    """输出确定性 SQL：既有 27 行按 id 排序补 aliases，83 条新增按 id 排序插入。"""
    existing = sorted(
        (row for row in rows if row["id"] in EXISTING_IDS), key=lambda row: row["id"]
    )
    new_rows = sorted(
        (row for row in rows if row["id"] not in EXISTING_IDS), key=lambda row: row["id"]
    )
    lines = [
        "-- 006_expand_exercise_catalog.sql",
        "-- 动作目录扩容（§1–§4）：新增 aliases_json 列；既有 27 行只补 aliases；",
        f"-- 插入 {NEW_TOTAL} 条新增动作（{RECOMMENDABLE} 可推荐 / {RECORD_ONLY} 仅记录）。",
        "-- 由 backend/tools/build_exercise_catalog.py 依据 backend/tools/exercise_catalog.json 与",
        f"-- exercises-dataset@{DATASET_COMMIT} 生成：不要手工编辑，改目录 JSON 后重跑脚本。",
        "-- 不含媒体 URL、instructions、target 与 muscle_group；既有行的 ID、行内容、",
        "-- recommendable、source_ref 与 attribution 一律不动。",
        "",
        "ALTER TABLE exercises",
        "ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'",
        "CHECK (json_valid(aliases_json));",
        "",
        "-- 既有 27 行的常见别名（通用别名只落在唯一默认动作上）",
    ]
    for row in existing:
        lines.append(
            "UPDATE exercises SET aliases_json = "
            f"{_json_literal(row['aliases'])} WHERE id = {_sql_literal(row['id'])};"
        )
    lines.append("")
    lines.append(
        f"-- {NEW_TOTAL} 条新增动作：source_ref 固定 {SOURCE_REF_PREFIX}:<source_id>，"
        "attribution 使用纯数据来源声明"
    )
    lines.append(_INSERT_HEADER.rstrip("\n"))
    values = []
    for row in new_rows:
        recommendation = "1" if row["recommendable"] else "0"
        increment = (
            "NULL"
            if row["min_load_increment_kg"] is None
            else repr(row["min_load_increment_kg"])
        )
        values.append(
            "    ("
            + ", ".join(
                [
                    _sql_literal(row["id"]),
                    _sql_literal(row["standard_name_zh"]),
                    _sql_literal(row["equipment_variant"]),
                    _sql_literal(row["record_type"]),
                    "NULL"
                    if row["load_convention"] is None
                    else _sql_literal(row["load_convention"]),
                    increment,
                    recommendation,
                    _json_literal(row["modes"]),
                    _json_literal(row["aliases"]),
                    _sql_literal(f"{SOURCE_REF_PREFIX}:{row['source_id']}"),
                    _sql_literal(NEW_ATTRIBUTION),
                ]
            )
            + ")"
        )
    lines.append(",\n".join(values) + ";")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 / 校验动作目录迁移 006")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验：重生成 SQL 并与已提交的 006 逐字节比较，不一致即非零退出",
    )
    args = parser.parse_args(argv)
    try:
        rows = _load_catalog()
        dataset = _load_dataset()
        validate(rows, dataset)
        sql = build_sql(rows)
    except CatalogError as error:
        print(f"目录校验失败：{error}", file=sys.stderr)
        return 1

    if args.check:
        try:
            committed = MIGRATION_PATH.read_text(encoding="utf-8")
        except OSError as error:
            print(f"迁移文件无法读取：{error}", file=sys.stderr)
            return 1
        if committed != sql:
            print(
                f"迁移文件与目录源文件不一致：{MIGRATION_PATH}（请重跑脚本生成）",
                file=sys.stderr,
            )
            return 1
        print(f"OK：{TOTAL} 条动作，{RECOMMENDABLE} 可推荐，{RECORD_ONLY} 仅记录，006 已同步")
        return 0

    MIGRATION_PATH.write_text(sql, encoding="utf-8")
    print(f"已写出 {MIGRATION_PATH}：{len(rows)} 条动作")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
