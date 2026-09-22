# 动作数据集的内存实现：一次性读入已入库的静态语料 exercises.zh-en.json，按文本与归一后的
# facet 做确定性检索。数据集只有约 1300 行，无需建表与迁移；写库的动作目录仍是另一个来源。

import json
from pathlib import Path
from typing import Any

from app.domain.actions.dataset import DatasetExercise, equivalent_values

#: 组合根默认读取的数据集路径：backend/data 下的已提交语料。
DEFAULT_DATASET_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "exercises.zh-en.json"
)


class InMemoryExerciseDataset:
    """加载一次、常驻内存的动作数据集检索实现。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_DATASET_PATH
        self._rows: tuple[DatasetExercise, ...] | None = None
        self._by_id: dict[str, DatasetExercise] = {}

    def _ensure_loaded(self) -> tuple[DatasetExercise, ...]:
        rows = self._rows
        if rows is None:
            rows = self._read()
            self._rows = rows
            self._by_id = {row.id: row for row in rows}
        return rows

    def _read(self) -> tuple[DatasetExercise, ...]:
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"数据集应为记录数组：{self._path}")
        records: list[dict[str, Any]] = raw
        if not records:
            raise ValueError(f"数据集为空：{self._path}")
        return tuple(DatasetExercise.from_row(record) for record in records)

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
        facets = {
            "body_part": body_part,
            "equipment": equipment,
            "target": target,
            "muscle_group": muscle_group,
        }
        active = {
            field: equivalent_values(field, str(value))
            for field, value in facets.items()
            if value is not None
        }
        matches = [
            row
            for row in self._ensure_loaded()
            if not (text and not row.matches_text(text))
            and all(row.facet_value(field) in want for field, want in active.items())
        ]
        matches.sort(key=lambda row: row.id)
        return tuple(matches[:limit])

    async def get_detail(self, exercise_id: str) -> DatasetExercise | None:
        """按数据集身份取一行；不存在即 None。"""
        self._ensure_loaded()
        return self._by_id.get(exercise_id)
