from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

# 上游完整库（含 category 与单段 instructions，agent.json 裁掉了这些）。
DEFAULT_SOURCE = Path(
    "//wsl.localhost/Ubuntu/home/finnian/code/github-repos/exercises-dataset/data/exercises.json"
)
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "data"

BODY_PARTS = {
    "back",
    "cardio",
    "chest",
    "lower arms",
    "lower legs",
    "neck",
    "shoulders",
    "upper arms",
    "upper legs",
    "waist",
}
# 只保留中英两语种；其余 8 语种连同媒体字段一并丢弃。
LANGS = ("zh", "en")


def _require_text(record: Mapping, field: str) -> str:
    value = record[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{record['id']} 的 {field} 缺失或为空")
    return value


def _project_language_map(record: Mapping, field: str) -> dict[str, str]:
    source = record[field]
    if not isinstance(source, Mapping):
        raise ValueError(f"{record['id']} 的 {field} 不是对象")
    projected: dict[str, str] = {}
    for lang in LANGS:
        value = source.get(lang)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{record['id']} 的 {field}.{lang} 缺失或为空")
        projected[lang] = value
    return projected


def _project_steps(record: Mapping) -> dict[str, list[str]]:
    source = record["instruction_steps"]
    if not isinstance(source, Mapping):
        raise ValueError(f"{record['id']} 的 instruction_steps 不是对象")
    projected: dict[str, list[str]] = {}
    for lang in LANGS:
        steps = source.get(lang)
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{record['id']} 的 instruction_steps.{lang} 缺失或为空数组")
        if not all(isinstance(step, str) and step.strip() for step in steps):
            raise ValueError(f"{record['id']} 的 instruction_steps.{lang} 含空串")
        projected[lang] = steps
    return projected


def project(record: Mapping) -> dict:
    body_part = _require_text(record, "body_part")
    if body_part not in BODY_PARTS:
        raise ValueError(f"{record['id']} 的 body_part 越界：{body_part!r}")
    secondary = record["secondary_muscles"]
    if not isinstance(secondary, list) or not all(
        isinstance(m, str) and m.strip() for m in secondary
    ):
        raise ValueError(f"{record['id']} 的 secondary_muscles 非法")
    return {
        "id": _require_text(record, "id"),
        "name": _require_text(record, "name"),
        "category": _require_text(record, "category"),
        "body_part": body_part,
        "equipment": _require_text(record, "equipment"),
        "target": _require_text(record, "target"),
        "muscle_group": _require_text(record, "muscle_group"),
        "secondary_muscles": list(secondary),
        "instructions": _project_language_map(record, "instructions"),
        "steps": _project_steps(record),
    }


def main(argv: list[str]) -> int:
    source = Path(argv[1]) if len(argv) > 1 else DEFAULT_SOURCE
    out_dir = Path(argv[2]) if len(argv) > 2 else DEFAULT_OUT_DIR
    records = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("源文件顶层必须是数组")

    projected = [project(record) for record in records]
    ids = [row["id"] for row in projected]
    names = [row["name"] for row in projected]
    if len(set(ids)) != len(ids):
        raise ValueError("投影后 id 出现重复：id 是稳定身份，必须唯一")
    # name 允许重复（上游确有同名不同 id 的动作），只作信息性统计，不作不变量。
    dup_names = len(names) - len(set(names))

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "exercises.zh-en.json"
    out_file.write_text(
        json.dumps(projected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"源记录 {len(records)} → 投影 {len(projected)}")
    print(f"输出 {out_file}（{out_file.stat().st_size} 字节）")
    print(f"id 唯一 True，同名不同 id 的动作数：{dup_names}")
    sample = projected[0]
    print("样例 id/name/category:", sample["id"], sample["name"], "/", sample["category"])
    print("instructions.langs:", sorted(sample["instructions"]), "steps.langs:", sorted(sample["steps"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
