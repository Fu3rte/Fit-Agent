"""复盘应用层 seam（S3-13）：保存／读取 Markdown 正文、生成时统计快照与精确来源修订引用。

正本：stage3.md §5 S3-13、§8 D6（**仅显式请求时**生成并保存，不自动按周生成）、
architecture/06 6.4。五条硬边界：

- **不生成正文**：Stage 3 不做模型生成（D6）；本 seam 只接收调用方（Stage 4 的显式请求）给出的
  Markdown，不拼模板、不写占位正文、不产生「像复盘」的假内容。
- **冻结来源与数值**：保存调用方当时现算出来的统计快照与**精确**来源修订 id（06 6.4）；不存
  「最新修订」指针、不在读时重解析，也绝不把快照回填进统计（当前统计一律现算，06 6.3）。
- **只追加**：重生成是新增一行，旧正文与旧快照既不改写也不删除（06 6.4「不静默改写」）。
- **stale 现算**：读旧复盘时按所引修订是否仍是该训练身份的当前修订现算 ``stale``；更正追加新
  修订并切换指针、作废同样（05 5.3），故旧复盘自然读到 stale，而正文与快照原样保留。
- **不推进业务版本**：复盘不是计划／记录事实，不写档案、不推进 ``context_version``、不建第二套
  版本计数器；也不需要草稿确认流程（06 6.4 的生成触发是显式请求本身）。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from domain.stats.review_repo import ReviewRecord, ReviewRepo
from domain.stats.rules import (
    InvalidReviewContent,
    review_basis_changed,
    validate_review_content,
)
from domain.stats.schema import ReviewStatSnapshot
from storage.db import Database


def _now() -> str:
    """生成时刻（UTC ISO 文本）：与记录确认同一口径，调用方不注入时间。"""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ReviewView:
    """一条复盘的只读投影：正文与生成时快照 + 现算 ``stale``（06 6.4）。

    ``stale`` 为真表示依据已变更、允许重新生成；旧正文与旧快照仍按生成时原样返回。
    """

    id: str
    body_markdown: str
    basis: ReviewStatSnapshot
    generated_at: str
    source_revision_ids: tuple[str, ...]
    stale: bool


def _view(record: ReviewRecord) -> ReviewView:
    return ReviewView(
        id=record.id,
        body_markdown=record.body_markdown,
        basis=record.basis,
        generated_at=record.generated_at,
        source_revision_ids=tuple(
            reference.session_revision_id for reference in record.references
        ),
        stale=review_basis_changed(record.references),
    )


class ReviewStore:
    """复审存取入口（无 HTTP 面）：显式保存一条复盘、按身份或按生成顺序读取。"""

    def __init__(self, db: Database):
        self._db = db
        self._reviews = ReviewRepo(db)

    async def save_review(
        self,
        *,
        body_markdown: str,
        basis: ReviewStatSnapshot,
        source_revision_ids: tuple[str, ...] = (),
    ) -> ReviewView:
        """追加保存一条复盘；返回保存结果（来源刚校验过，故 ``stale`` 为假）。

        ``source_revision_ids`` 是这次快照所依据的**精确**训练修订 id：每一条都必须存在，且必须
        是该训练身份的当前修订——指向旧修订或不存在修订一律拒绝（否则新复盘一落库就 stale，或
        引用一个不存在的来源）。校验与写入在同一事务内完成，任一步失败零写入。
        """
        validate_review_content(
            body_markdown=body_markdown, source_revision_ids=source_revision_ids
        )
        review_id = uuid4().hex
        generated_at = _now()
        async with self._db.transaction() as conn:
            for revision_id in source_revision_ids:
                reference = await self._reviews.resolve_reference_in_transaction(
                    conn, revision_id
                )
                if reference is None:
                    raise InvalidReviewContent(f"复盘来源修订不存在：{revision_id}")
                if reference.current_revision_id != revision_id:
                    raise InvalidReviewContent(
                        f"复盘来源修订已不是该训练的当前修订：{revision_id}"
                    )
            await self._reviews.append_review_in_transaction(
                conn,
                review_id=review_id,
                body_markdown=body_markdown,
                basis=basis,
                source_revision_ids=source_revision_ids,
                generated_at=generated_at,
            )
        return ReviewView(
            id=review_id,
            body_markdown=body_markdown,
            basis=basis,
            generated_at=generated_at,
            source_revision_ids=source_revision_ids,
            stale=False,
        )

    async def read_review(self, review_id: str) -> ReviewView | None:
        """按身份读取一条复盘；不存在即 ``None``，正文／快照按生成时原样返回。"""
        async with self._db.transaction() as conn:
            record = await self._reviews.read_review_in_transaction(conn, review_id)
        return None if record is None else _view(record)

    async def list_reviews(self) -> tuple[ReviewView, ...]:
        """按生成时刻列出全部复盘（追加语义：重生成在后，旧复盘仍可读）。"""
        async with self._db.transaction() as conn:
            records = await self._reviews.list_reviews_in_transaction(conn)
        return tuple(_view(record) for record in records)
