"""Stage 6 费用账本的持久化（08「Stage 6 联调费用护栏」；016 迁移）。

一个 stage 一行：``limit_usd`` 由调用方（代码常量）每次写回，``spent_usd`` 与
``reserved_usd`` 是账本历史，跨重启累计、不重置。余额 = 额度 − 已花费 − 在途预留；
``reserve`` 是唯一放行点，余额不足不写账本、返回 ``False``（调用方不得发送请求）。

SQL 全部在本 repo 内以字面量书写并参数化（README 硬规则 3）；本层不打印金额以外的任何
业务数据，也不读取 Provider Key。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime

import aiosqlite

from storage.db import Database

_LEDGER_COLUMNS = "stage, limit_usd, spent_usd, reserved_usd, updated_at"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_stage(stage: object) -> str:
    if not isinstance(stage, str) or not stage.strip():
        raise ValueError("stage 必须是非空字符串")
    return stage


def _require_amount(name: str, value: object) -> float:
    """金额校验：必须是有限非负数（NaN／±Inf 参与比较会静默失真，一律拒绝）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数值")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{name} 必须是有限非负数")
    return amount


def _projection(row: Mapping[str, object] | None, limit_usd: float) -> dict[str, float]:
    """账本快照：SQLite 数值列显式转 float，避免以 int 形态参与后续算术。"""
    spent = 0.0 if row is None else float(row["spent_usd"])  # type: ignore[arg-type]
    reserved = 0.0 if row is None else float(row["reserved_usd"])  # type: ignore[arg-type]
    return {
        "limit_usd": limit_usd,
        "spent_usd": spent,
        "reserved_usd": reserved,
        "available_usd": limit_usd - spent - reserved,
    }


class FeeRepo:
    """费用账本存取；访问经 Database 唯一锁串行化（07 7.1）。"""

    def __init__(self, db: Database):
        self._db = db

    async def snapshot(self, stage: str, *, limit_usd: float) -> dict[str, float]:
        """只读快照；不创建行、不改余额。"""
        stage = _require_stage(stage)
        limit = _require_amount("limit_usd", limit_usd)

        async def op(conn: aiosqlite.Connection) -> aiosqlite.Row | None:
            async with conn.execute(
                f"SELECT {_LEDGER_COLUMNS} FROM fee_ledger WHERE stage = ?", (stage,)
            ) as cursor:
                return await cursor.fetchone()

        row = await self._db.under_lock(op)
        return _projection(None if row is None else dict(row), limit)

    async def reserve(self, stage: str, *, amount_usd: float, limit_usd: float) -> bool:
        """预留一笔费用上界：余额足够则入账并返回 ``True``，否则不改账本返回 ``False``。"""
        stage = _require_stage(stage)
        amount = _require_amount("amount_usd", amount_usd)
        limit = _require_amount("limit_usd", limit_usd)
        async with self._db.transaction() as conn:
            row = await self._row_locked(conn, stage)
            current = _projection(None if row is None else dict(row), limit)
            if amount <= 0 or current["available_usd"] < amount:
                return False
            if row is None:
                await conn.execute(
                    "INSERT INTO fee_ledger"
                    f" ({_LEDGER_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                    (stage, limit, 0.0, amount, _now()),
                )
            else:
                await conn.execute(
                    "UPDATE fee_ledger SET limit_usd = ?, reserved_usd = reserved_usd + ?,"
                    " updated_at = ? WHERE stage = ?",
                    (limit, amount, _now(), stage),
                )
        return True

    async def settle(
        self,
        stage: str,
        *,
        reserved_usd: float,
        charged_usd: float,
        limit_usd: float,
    ) -> dict[str, float]:
        """结算一笔在途预留：归还预留额并把真实花费（或未知 usage 的预留额）计入已花费。

        结算额在任何情况下都不得低于调用方声明的预留额之外的东西；预留额本身按传入值
        归零（下界 0），已花费只增不减（不冲销历史费用）。
        """
        stage = _require_stage(stage)
        reserved = _require_amount("reserved_usd", reserved_usd)
        charged = _require_amount("charged_usd", charged_usd)
        limit = _require_amount("limit_usd", limit_usd)
        async with self._db.transaction() as conn:
            row = await self._row_locked(conn, stage)
            if row is None:
                raise ValueError(f"费用账本没有 stage={stage} 的行：拒绝结算")
            await conn.execute(
                "UPDATE fee_ledger SET limit_usd = ?,"
                " reserved_usd = MAX(0.0, reserved_usd - ?),"
                " spent_usd = spent_usd + ?, updated_at = ? WHERE stage = ?",
                (limit, reserved, charged, _now(), stage),
            )
            updated = await self._row_locked(conn, stage)
        return _projection(None if updated is None else dict(updated), limit)

    async def settle_orphan_reservations(
        self, stage: str, *, limit_usd: float
    ) -> dict[str, float]:
        """把崩溃遗留的在途预留按未知 usage 扣账（08：不释放其预留额）。

        只由 Run 开始时调用（全局单 Run：同一进程内最多一笔在途预留，Run 开始时仍在途的
        预留必然来自上一次崩溃或强杀）。
        """
        stage = _require_stage(stage)
        limit = _require_amount("limit_usd", limit_usd)
        async with self._db.transaction() as conn:
            row = await self._row_locked(conn, stage)
            if row is None:
                return _projection(None, limit)
            await conn.execute(
                "UPDATE fee_ledger SET limit_usd = ?, spent_usd = spent_usd + reserved_usd,"
                " reserved_usd = 0.0, updated_at = ? WHERE stage = ?",
                (limit, _now(), stage),
            )
            updated = await self._row_locked(conn, stage)
        return _projection(None if updated is None else dict(updated), limit)

    async def _row_locked(
        self, conn: aiosqlite.Connection, stage: str
    ) -> aiosqlite.Row | None:
        async with conn.execute(
            f"SELECT {_LEDGER_COLUMNS} FROM fee_ledger WHERE stage = ?", (stage,)
        ) as cursor:
            return await cursor.fetchone()
