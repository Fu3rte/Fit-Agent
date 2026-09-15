"""plans 用例编排：计划版本与计划日程的只读查询（Stage 1 子任务 02 §8；REFACTOR_PLAN §11）。

边界：本服务**只读**，是 ``PlanRepo`` 的用例入口，供表单 API 直接消费（``api/routes_plans.py``）
——不经过 Agent、不创建草稿。不提供创建、确认、拒绝、激活或归档入口；计划激活事务留到 Stage 5，
本阶段测试与用例只用直接 SQL 写入计划数据。
"""

from domain.plans.repo import PlanRepo
from domain.plans.schema import Plan, PlanSession
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
