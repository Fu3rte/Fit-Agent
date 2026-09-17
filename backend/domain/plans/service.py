"""plans 用例编排：计划版本与计划日程的只读查询（Stage 1 子任务 02 §8；REFACTOR_PLAN §11）与 Stage 4
最小持久化（stage4.md §3.8–§3.10、§5.2、§6 Subtask 03）。

- :class:`PlanReadService` **只读**，是 ``PlanRepo`` 的用例入口，供表单 API 直接消费
  （``api/routes_plans.py``）——不经过 Agent、不创建草稿。
- :class:`PlanPersistenceService` 只做 §3.9 的业务写入：事务内分配版本号并插入 draft／rejected，
  或在显式重新生成通过后条件替换同一 draft；不含模型调用（模型调用在事务外，由 Graph 节点负责），
  不自动激活、不归档原 active、不建计划日程（留 Stage 5）。
"""

from aiosqlite import Connection

from domain.plans.repo import ActivePlanSnapshot, PlanRepo
from domain.plans.schema import (
    EvaluationResult,
    Plan,
    PlanDraft,
    PlanSession,
    evaluation_result_to_json,
    plan_draft_to_json,
)
from storage.db import Database


class PlanReadService:
    """计划只读用例：当前 active、draft、历史版本与计划日程。"""

    def __init__(self, db: Database):
        self._repo = PlanRepo(db)

    async def get_active(self) -> Plan | None:
        """当前 active 计划；没有正式启用的计划时返回 None（不拿 draft 当替代）。"""
        return await self._repo.read_active()

    async def get_by_id(self, plan_id: int) -> Plan | None:
        """按身份读取一个计划版本（含历史版本）；不存在即 None。"""
        return await self._repo.read_by_id(plan_id)

    async def list_drafts(self) -> tuple[Plan, ...]:
        """全部 draft 计划（按版本号升序）。"""
        return await self._repo.list_drafts()

    async def list_versions(self) -> tuple[Plan, ...]:
        """全部计划版本（按版本号升序）：已归档的历史版本从此读取。"""
        return await self._repo.list_versions()

    async def list_sessions(self, plan_id: int) -> tuple[PlanSession, ...]:
        """某个计划的全部日程（含已取消行，按应训练日排序）。"""
        return await self._repo.list_sessions(plan_id)


class PlanDraftConflict(ValueError):
    """条件更新未命中，或原 draft 已不存在或不再是 draft：状态已变化，明确冲突，不覆盖数据。"""


class PlanPersistenceService:
    """Stage 4 计划持久化：draft/rejected 最小写路径与原 active 保护（stage4.md §3.8–§3.10、§9）。

    调用方（Graph 节点）在事务外完成生成与评估：

    - 已有 draft 且是普通生成请求：先取 :meth:`get_unique_draft`，直接返回它，不调模型；
    - 显式重新生成：候选只留在 State／checkpoint，通过后才 :meth:`persist_plan_result` 条件替换。

    每个写路径在事务内前后比对原 active 快照（id／状态／版本／内容／确认时间 + 行数）：Stage 4 全程不
    修改原 active（stage4.md §9.2）。写失败即整个短事务回滚。
    """

    def __init__(self, db: Database):
        self._db = db
        self._repo = PlanRepo(db)

    async def get_unique_draft(self) -> Plan | None:
        """当前唯一可确认 draft；没有即 None（已有时普通请求直接返回它，不调模型）。"""
        return await self._repo.read_draft()

    async def persist_plan_result(
        self,
        draft: PlanDraft,
        evaluation: EvaluationResult,
        *,
        existing_draft_id: int | None,
        created_at: str,
    ) -> Plan:
        """按 §3.9 落到唯一正确的业务写入，返回写入（或保持不变的）计划行。

        - 通过 + 无原 draft：插入一条 draft
        - 通过 + 有原 draft（显式重新生成）：在同一 id/version 上条件替换；未命中抛
          :class:`PlanDraftConflict`
        - 阻断失败 + 无原 draft：插入一条 rejected
        - 阻断失败 + 有原 draft：原 draft 不变，不写 rejected（失败候选只留 State／checkpoint）
        """
        content_json = plan_draft_to_json(draft)
        result_json = evaluation_result_to_json(evaluation)
        if evaluation.passed:
            return await self._persist_passing(
                content_json,
                result_json,
                existing_draft_id=existing_draft_id,
                created_at=created_at,
            )
        return await self._persist_blocking_failure(
            content_json,
            result_json,
            existing_draft_id=existing_draft_id,
            created_at=created_at,
        )

    async def _persist_passing(
        self,
        content_json: str,
        result_json: str,
        *,
        existing_draft_id: int | None,
        created_at: str,
    ) -> Plan:
        """通过路径：无原 draft 则插入，有原 draft 则条件替换同一 id/version。"""
        async with self._db.transaction() as conn:
            before = await self._repo.read_active_snapshot_in_transaction(conn)
            if existing_draft_id is None:
                written = await self._repo.write_draft_in_transaction(
                    conn,
                    structured_content_json=content_json,
                    evaluator_result_json=result_json,
                    created_at=created_at,
                )
            else:
                written = await self._repo.replace_draft_in_transaction(
                    conn,
                    existing_draft_id,
                    structured_content_json=content_json,
                    evaluator_result_json=result_json,
                )
                if written is None:
                    raise PlanDraftConflict(
                        f"draft 已变化（不存在或不再是 draft），不覆盖：{existing_draft_id}"
                    )
            await self._require_active_unchanged(conn, before)
            return written

    async def _persist_blocking_failure(
        self,
        content_json: str,
        result_json: str,
        *,
        existing_draft_id: int | None,
        created_at: str,
    ) -> Plan:
        """阻断失败路径：无原 draft 写 rejected；有原 draft 则保持原 draft，不写任何行。"""
        if existing_draft_id is not None:
            existing = await self._repo.read_by_id(existing_draft_id)
            if existing is None or existing.status != "draft":
                raise PlanDraftConflict(
                    f"原 draft 不存在或不再是 draft：{existing_draft_id}"
                )
            return existing
        async with self._db.transaction() as conn:
            before = await self._repo.read_active_snapshot_in_transaction(conn)
            written = await self._repo.write_rejected_in_transaction(
                conn,
                structured_content_json=content_json,
                evaluator_result_json=result_json,
                created_at=created_at,
            )
            await self._require_active_unchanged(conn, before)
            return written

    async def _require_active_unchanged(
        self, conn: Connection, before: ActivePlanSnapshot
    ) -> None:
        """写路径结束时原 active 必须逐字段不变（stage4.md §9.2）：变了即回滚并大声失败。"""
        after = await self._repo.read_active_snapshot_in_transaction(conn)
        if after != before:
            raise RuntimeError(
                f"写路径修改了原 active 计划（stage4.md §9.2）：{before!r} -> {after!r}"
            )
