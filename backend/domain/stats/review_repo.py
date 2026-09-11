"""复盘存储手写 SQL（正本 architecture/06 6.4、stage3.md §5 S3-13）。

复盘不是「正式训练事实」，而是对确定性统计的解释性产物：正文（Markdown）与生成时的统计依据
快照一起保存，**只追加**——重生成是新增一行，旧行不覆盖、不 UPDATE。因此本模块：

- 只提供 INSERT 与读取，没有任何 UPDATE／DELETE 语句；
- 写入只接受外层 ``transaction()`` 连接（``require_outer_transaction``），由
  :class:`~app.review_store.ReviewStore` 在同一事务内先校验来源修订再落库；
- 精确来源修订引用存 ``review_source_revisions`` 行（外键保证引用存在）；读回时一并取该训练
  身份的当刻当前修订，供 :func:`~domain.stats.rules.review_basis_changed` 现算 stale——
  不回写旧行、不把快照当当前统计输入（06 6.3／6.4）。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import aiosqlite

from domain.stats.schema import (
    ReviewSourceRevisions,
    ReviewStatSnapshot,
    review_basis_from_json,
    review_basis_to_json,
)
from storage.db import Database, require_outer_transaction

_SELECT_REVIEW = (
    "SELECT r.id, r.body_markdown, r.basis_json, r.generated_at,"
    " ref.session_revision_id, sr.session_id, s.current_revision_id"
    " FROM reviews AS r"
    " LEFT JOIN review_source_revisions AS ref ON ref.review_id = r.id"
    " LEFT JOIN session_revisions AS sr ON sr.id = ref.session_revision_id"
    " LEFT JOIN training_sessions AS s ON s.id = sr.session_id"
)


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """一条复盘行 + 它的精确来源修订引用（含各训练身份的当刻当前修订）。"""

    id: str
    body_markdown: str
    basis: ReviewStatSnapshot
    generated_at: str
    references: tuple[ReviewSourceRevisions, ...]


def _rows_to_reviews(rows: Iterable[aiosqlite.Row]) -> tuple[ReviewRecord, ...]:
    """左连接结果 → 复盘记录：每行主体只出现一次，来源引用按行累积（顺序即插入顺序）。"""
    bodies: dict[str, dict[str, Any]] = {}
    references: dict[str, list[ReviewSourceRevisions]] = {}
    for row in rows:
        review_id = str(row["id"])
        if review_id not in bodies:
            bodies[review_id] = {
                "body_markdown": str(row["body_markdown"]),
                "basis": review_basis_from_json(str(row["basis_json"])),
                "generated_at": str(row["generated_at"]),
            }
            references[review_id] = []
        if row["session_revision_id"] is not None:
            references[review_id].append(_reference_from_row(row))
    return tuple(
        ReviewRecord(
            id=review_id,
            body_markdown=str(body["body_markdown"]),
            basis=body["basis"],
            generated_at=str(body["generated_at"]),
            references=tuple(references[review_id]),
        )
        for review_id, body in bodies.items()
    )


def _reference_from_row(row: aiosqlite.Row) -> ReviewSourceRevisions:
    """来源引用行：修订与训练身份由主键 + 外键保证存在，缺失即库内状态损坏（显式失败）。"""
    revision_id = row["session_revision_id"]
    session_id = row["session_id"]
    if revision_id is None or session_id is None:
        raise RuntimeError(f"复盘来源引用缺少对应修订行：{row['id']}")
    current = row["current_revision_id"]
    return ReviewSourceRevisions(
        session_revision_id=str(revision_id),
        session_id=str(session_id),
        current_revision_id=None if current is None else str(current),
    )


class ReviewRepo:
    """复盘只读＋追加：无 UPDATE、无 DELETE，读取恒按存储的精确引用（06 6.4）。"""

    def __init__(self, db: Database):
        self._db = db

    async def resolve_reference_in_transaction(
        self, conn: aiosqlite.Connection, revision_id: str
    ) -> ReviewSourceRevisions | None:
        """解析一条来源修订引用；修订不存在即 ``None``（由调用方拒绝保存，不静默接受）。

        同时取出该训练身份的当刻当前修订，供调用方在保存时拒掉「已不是当前修订」的引用
        （否则新存的复盘立刻显示 stale）。
        """
        require_outer_transaction(conn, "复盘来源修订解析")
        async with conn.execute(
            "SELECT r.id AS session_revision_id, r.session_id,"
            " s.current_revision_id FROM session_revisions AS r"
            " JOIN training_sessions AS s ON s.id = r.session_id"
            " WHERE r.id = ?",
            (revision_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        current = row["current_revision_id"]
        return ReviewSourceRevisions(
            session_revision_id=str(row["session_revision_id"]),
            session_id=str(row["session_id"]),
            current_revision_id=None if current is None else str(current),
        )

    async def append_review_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        review_id: str,
        body_markdown: str,
        basis: ReviewStatSnapshot,
        source_revision_ids: tuple[str, ...],
        generated_at: str,
    ) -> None:
        """追加一条复盘（正文 + 快照 + 精确来源引用）；不覆盖任何既有行（06 6.4）。"""
        require_outer_transaction(conn, "复盘追加写入")
        await conn.execute(
            "INSERT INTO reviews (id, body_markdown, basis_json, generated_at)"
            " VALUES (?, ?, ?, ?)",
            (
                review_id,
                body_markdown,
                review_basis_to_json(basis),
                generated_at,
            ),
        )
        for revision_id in source_revision_ids:
            await conn.execute(
                "INSERT INTO review_source_revisions (review_id, session_revision_id)"
                " VALUES (?, ?)",
                (review_id, revision_id),
            )

    async def read_review_in_transaction(
        self, conn: aiosqlite.Connection, review_id: str
    ) -> ReviewRecord | None:
        """按身份读取一条复盘（含精确来源引用与各身份的当刻当前修订）；不存在即 ``None``。"""
        require_outer_transaction(conn, "复盘读取")
        async with conn.execute(
            f"{_SELECT_REVIEW} WHERE r.id = ?", (review_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        records = _rows_to_reviews(rows)
        if len(records) > 1:  # 主键冲突的数据损坏，不静默取第一条
            raise RuntimeError(f"复盘 id 命中多行：{review_id}")
        return None if not records else records[0]

    async def list_reviews_in_transaction(
        self, conn: aiosqlite.Connection
    ) -> tuple[ReviewRecord, ...]:
        """按生成时刻列出全部复盘（追加语义：重生成在后，不覆盖在前）。"""
        require_outer_transaction(conn, "复盘列表读取")
        async with conn.execute(
            f"{_SELECT_REVIEW} ORDER BY r.generated_at, r.id"
        ) as cursor:
            return _rows_to_reviews(await cursor.fetchall())

    async def read_review(self, review_id: str) -> ReviewRecord | None:
        """锁内读取一条复盘（只读查询，自成一次锁内操作）。"""
        async with self._db.transaction() as conn:
            return await self.read_review_in_transaction(conn, review_id)

    async def list_reviews(self) -> tuple[ReviewRecord, ...]:
        """锁内按生成时刻列出全部复盘。"""
        async with self._db.transaction() as conn:
            return await self.list_reviews_in_transaction(conn)
