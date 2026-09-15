"""actions 业务表手写 SQL：exercises 的目录全量与按稳定身份读取（Stage 1 子任务 02 §4）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）；本层只读——动作种子由编号迁移
``001_initial.sql`` 写入，领域层不提供目录写入口，也不在启动路径覆盖目录。

读取出口只两个：目录全量与按稳定身份取一行。读到即校验 ``modes_json`` 元素落在 13 项词表内：
库内 CHECK 只保证 ``json_valid``，越界行属目录数据损坏，必须大声失败而不是流给计划链路。
SQL 一律以字面量书写并参数化（storage/README 硬规则 3）。
"""

import aiosqlite

from domain.actions.rules import validate_modes
from domain.actions.schema import Exercise
from storage.db import Database

#: exercises 列清单：本模块所有读取共用同一份，避免漏列（列顺序即 Exercise.from_row 读取的键）。
_COLUMNS = (
    "id, standard_name_zh, equipment_variant, record_type, load_convention,"
    " min_load_increment_kg, recommendable, modes_json, source_ref, attribution"
)


def _row_to_exercise(row: aiosqlite.Row) -> Exercise:
    """行 → 动作身份，并复查模式词表（唯一取值域在 rules）。"""
    exercise = Exercise.from_row(dict(row))
    validate_modes(exercise.modes)
    return exercise


async def _read_by_id(
    conn: aiosqlite.Connection, exercise_id: str
) -> Exercise | None:
    async with conn.execute(
        "SELECT " + _COLUMNS + " FROM exercises WHERE id = ?", (exercise_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _row_to_exercise(row)


async def _read_all(conn: aiosqlite.Connection) -> tuple[Exercise, ...]:
    async with conn.execute("SELECT " + _COLUMNS + " FROM exercises ORDER BY id") as cursor:
        rows = await cursor.fetchall()
    return tuple(_row_to_exercise(row) for row in rows)


class ExerciseRepo:
    """exercises 表读取；写入口只在编号迁移内（本类不提供）。"""

    def __init__(self, db: Database):
        self._db = db

    async def get_by_id(self, exercise_id: str) -> Exercise | None:
        """按稳定身份读取；不存在即 None。"""
        return await self._db.under_lock(lambda conn: _read_by_id(conn, exercise_id))

    async def list_all(self) -> tuple[Exercise, ...]:
        """目录全量（按稳定身份排序）。"""
        return await self._db.under_lock(_read_all)
