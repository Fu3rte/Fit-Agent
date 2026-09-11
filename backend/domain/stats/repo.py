"""stats 只读查询：``pr_candidates`` 视图上的现算 PR（正本 architecture/06 6.3）。

PR 不存结果（06 6.3）：每次查询都从迁移 010 的视图现算，因此更正／作废后立即反映最新有效
事实（06 6.4）。候选过滤条件（当前有效修订、排除待补全／作废／回归期／热身／人工辅助、
目录记录词表、负重与次数明确）只在视图里写一份，本层不重复第二套过滤条件。
"""

import aiosqlite

from domain.stats.schema import InvalidReviewRow
from storage.db import Database


def _column_int(label: str, raw: object) -> int:
    """视图整数列 → int；非整数存储类即数据损坏，不用 ``int()`` 静默截断。

    SQLite 列类型只是亲和：INTEGER 亲和列可以存下 REAL ``3.5`` 与 TEXT ``'abc'``（候选过滤
    条件放行），裸 ``int()`` 会把前者静默读成 3、后者抛 ValueError。
    """
    # bool 是 int 的子类但不表示数量，与 domain.stats.schema._require_int 同口径拒绝
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidReviewRow(f"{label} 必须是整数：{raw!r}")
    return raw


def _single_value(row: aiosqlite.Row | None) -> int | None:
    """聚合结果行 → 整数或 None（无候选是 None，不是 0）。"""
    if row is None:  # 聚合查询恒返回一行；缺失即连接异常
        raise RuntimeError("PR 聚合查询没有返回行")
    return None if row["value"] is None else _column_int("PR 聚合值", row["value"])


class StatsRepo:
    """``pr_candidates`` 上的只读聚合；不写任何表、不缓存 PR 结果。

    两条查询都按「同一动作＋负重口径（器械变式）」再按换算整数键比较（06 6.3）：换算键是
    009 的派生列，不同口径结构上不可比。
    """

    def __init__(self, db: Database):
        self._db = db

    async def pr_max_load(self, *, exercise_id: str, load_notation: str) -> int | None:
        """该动作与负重口径下的最高重量（换算整数键）；无候选即 None。"""

        async def op(conn: aiosqlite.Connection) -> int | None:
            async with conn.execute(
                "SELECT MAX(load_kg_key) AS value FROM pr_candidates"
                " WHERE exercise_id = ? AND load_notation = ?",
                (exercise_id, load_notation),
            ) as cursor:
                return _single_value(await cursor.fetchone())

        return await self._db.under_lock(op)

    async def pr_max_reps_at_load(
        self, *, exercise_id: str, load_notation: str, load_kg_key: int
    ) -> int | None:
        """该动作、口径与重量下的**单组**最高次数（同重量不累计多组，06 验收 7）。"""

        async def op(conn: aiosqlite.Connection) -> int | None:
            async with conn.execute(
                "SELECT MAX(reps) AS value FROM pr_candidates"
                " WHERE exercise_id = ? AND load_notation = ? AND load_kg_key = ?",
                (exercise_id, load_notation, load_kg_key),
            ) as cursor:
                return _single_value(await cursor.fetchone())

        return await self._db.under_lock(op)
