# 动作数据集的读取端口与内存实现：一次性读入已入库的静态语料 exercises.zh-en.json，按文本与归一后的
# facet 做确定性检索。数据集只有约 1300 行，无需建表与迁移。ExerciseDataset 是与工具同目录的可注入
# seam（对应 pi 的 ReadOperations）：默认用内存实现，将来换成基于表的实现只需另提供一个满足该协议的
# 对象并由组合根注入，工具与词表都不动。

import json
from pathlib import Path
from typing import Protocol

from app.application.agent.harness.tools.exercise_dataset.vocab import (
    DatasetExercise,
    equivalent_values,
)

#: 组合根默认读取的数据集路径：backend/data 下的已提交语料。
DEFAULT_DATASET_PATH = (
    Path(__file__).resolve().parents[6] / "data" / "exercises.zh-en.json"
)


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

    async def get_detail(self, exercise_id: str) -> DatasetExercise | None:
        """按数据集身份（数字串）取一行；不存在即 None。"""


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
