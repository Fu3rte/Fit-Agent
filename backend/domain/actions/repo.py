"""actions 业务表手写 SQL：按身份读取、别名候选、推荐候选（正本 architecture/03）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）；本层只读，动作种子
由编号迁移（``storage/migrations/003_*.sql``）写入，本层不提供目录写入口，也不
在启动路径全量覆盖目录。查询是最小精确匹配（标准名相等 / 别名相等，忽略大小写与
首尾空白），不引入语义或模糊搜索（stage1.md §5 S1-03「工作」）。
普通查询走唯一锁；:meth:`ExerciseRepo.get_by_id_in_transaction` 只接受外层 ``transaction()``
给出的连接，供确认事务（S2-05）在同一事务内复查限制引用的动作身份（stage2.md §2：
事务内不得调用自取锁方法，锁不可重入）。
SQL 一律以字面量书写并参数化（storage/README 硬规则 3）。
"""

import aiosqlite

from domain.actions.schema import Exercise
from storage.db import Database, require_outer_transaction


async def _read_by_id(conn: aiosqlite.Connection, exercise_id: str) -> Exercise | None:
    """用给定连接按稳定身份读取；连接由调用方决定（语句为字面量并参数化）。"""
    async with conn.execute(
        "SELECT id, standard_name_zh, equipment_variant, record_type,"
        " load_convention, unilateral, recommendable, active, aliases_json,"
        " modes_json, source_ref, attribution, instructions_zh"
        " FROM exercises WHERE id = ?",
        (exercise_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else Exercise.from_row(dict(row))


async def _read_recommendable(conn: aiosqlite.Connection) -> tuple[Exercise, ...]:
    """用给定连接读推荐候选（未停用且已检查标记可推荐）；连接由调用方决定。"""
    async with conn.execute(
        "SELECT id, standard_name_zh, equipment_variant, record_type,"
        " load_convention, unilateral, recommendable, active, aliases_json,"
        " modes_json, source_ref, attribution, instructions_zh"
        " FROM exercises WHERE active = 1 AND recommendable = 1 ORDER BY id"
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(Exercise.from_row(dict(row)) for row in rows)


class ExerciseRepo:
    """exercises 表读取；写入口只在编号迁移内（不在本类提供）。"""

    def __init__(self, db: Database):
        self._db = db

    async def get_by_id(self, exercise_id: str) -> Exercise | None:
        """按稳定身份读取；停用（active=0）动作同样返回，保留原有语义。"""

        async def op(conn: aiosqlite.Connection) -> Exercise | None:
            return await _read_by_id(conn, exercise_id)

        return await self._db.under_lock(op)

    async def get_by_id_in_transaction(
        self, conn: aiosqlite.Connection, exercise_id: str
    ) -> Exercise | None:
        """在**外层事务**内按稳定身份读取限制引用的动作（含停用动作）。

        ``conn`` 必须是 ``Database.transaction()`` 交出的连接：确认事务要在同一事务内复查
        限制引用，而 :meth:`get_by_id` 会自行取锁（锁不可重入，事务内调用即死锁）。本方法
        不自取锁、不 BEGIN／COMMIT，读到的是该事务连接自己的快照。
        """
        require_outer_transaction(conn, "动作引用读取")
        return await _read_by_id(conn, exercise_id)

    async def get_by_standard_name(self, standard_name: str) -> Exercise | None:
        """按中文标准名精确读取（一个标准名一个身份）。"""

        async def op(conn: aiosqlite.Connection) -> Exercise | None:
            async with conn.execute(
                "SELECT id, standard_name_zh, equipment_variant, record_type,"
                " load_convention, unilateral, recommendable, active, aliases_json,"
                " modes_json, source_ref, attribution, instructions_zh"
                " FROM exercises WHERE standard_name_zh = ?",
                (standard_name,),
            ) as cursor:
                row = await cursor.fetchone()
            return None if row is None else Exercise.from_row(dict(row))

        return await self._db.under_lock(op)

    async def find_by_alias(self, alias: str) -> tuple[Exercise, ...]:
        """按别名精确匹配，返回全部命中身份（含停用动作）；不静默取第一项。"""

        async def op(conn: aiosqlite.Connection) -> tuple[Exercise, ...]:
            async with conn.execute(
                "SELECT id, standard_name_zh, equipment_variant, record_type,"
                " load_convention, unilateral, recommendable, active, aliases_json,"
                " modes_json, source_ref, attribution, instructions_zh"
                " FROM exercises WHERE EXISTS ("
                "   SELECT 1 FROM json_each(exercises.aliases_json)"
                "   WHERE lower(trim(json_each.value)) = lower(trim(?)))"
                " ORDER BY id",
                (alias,),
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(Exercise.from_row(dict(row)) for row in rows)

        return await self._db.under_lock(op)

    async def list_recommendable(self) -> tuple[Exercise, ...]:
        """推荐候选：未停用且已检查标记可推荐（03 3.2；未检查不得自动进入）。"""
        return await self._db.under_lock(_read_recommendable)

    async def list_recommendable_in_transaction(
        self, conn: aiosqlite.Connection
    ) -> tuple[Exercise, ...]:
        """在**外层事务**内读推荐候选（同一快照，不嵌套取锁）。

        ``conn`` 必须是 ``Database.transaction()`` 交出的连接：生成输入准备要在同一快照内
        读档案、候选与当前计划；:meth:`list_recommendable` 会自行取锁（锁不可重入），
        事务内调用即死锁。
        """
        require_outer_transaction(conn, "推荐候选读取")
        return await _read_recommendable(conn)

    async def list_all(self) -> tuple[Exercise, ...]:
        """目录全量（含停用）；仅用于目录核对与统计，不用于推荐。"""

        async def op(conn: aiosqlite.Connection) -> tuple[Exercise, ...]:
            async with conn.execute(
                "SELECT id, standard_name_zh, equipment_variant, record_type,"
                " load_convention, unilateral, recommendable, active, aliases_json,"
                " modes_json, source_ref, attribution, instructions_zh"
                " FROM exercises ORDER BY id"
            ) as cursor:
                rows = await cursor.fetchall()
            return tuple(Exercise.from_row(dict(row)) for row in rows)

        return await self._db.under_lock(op)

    async def count(self) -> int:
        async def op(conn: aiosqlite.Connection) -> int:
            async with conn.execute("SELECT COUNT(*) FROM exercises") as cursor:
                row = await cursor.fetchone()
            if row is None:  # COUNT(*) 恒有结果行；缺失即连接状态异常
                raise RuntimeError("SELECT COUNT(*) 未返回结果行")
            return int(row[0])

        return await self._db.under_lock(op)
