"""Run 应用服务：请求幂等、全局单 Run、手动重试与重启恢复（08 8.2/8.4；S4-02）。

只做应用层语义判定与编排；SQL 与事务边界留在 :class:`~storage.run_repo.RunRepo`
（README 硬规则 3），因此“先按 ``client_request_id`` 查重、再判全局活跃 Run”发生在
同一事务里：重复或并发的同一请求返回已有 Run，不会被误报为 ``conversation_busy``。

本层不启动模型调用、不接 SSE；取消与 draining 的调度实现归 S4-03（``runtime/run_task.py``），
本层只额外接收一个**进程内名额探针**：取消后底层调用尚未退出时库内已无活跃 Run，
新请求仍须按 08 8.2 拒绝为 ``ConversationBusy``（幂等查重仍先于 busy）。

创建只落“用户请求 + pending Run”，停在可被 S4-03 驱动的起点。
"""

from collections.abc import Callable
from typing import Any

from app.draft_repo import DraftRepo
from app.drafts import DraftNotCorrectable, DraftService
from runtime.error_codes import INTERRUPTED_BY_RESTART, ORDINARY_FAILURE_CODES
from storage.errors import InvalidInput, NotFound
from storage.run_repo import RunRepo

RunRecord = dict[str, Any]


class RunService:
    """全局单 Run 的请求入口（HTTP 与执行驱动的唯一前置编排）。"""

    _repo: RunRepo
    _active_execution: Callable[[], str | None] | None
    _drafts: DraftRepo | None
    _baseline: DraftService | None

    def __init__(
        self,
        repo: RunRepo,
        *,
        active_execution: Callable[[], str | None] | None = None,
        drafts: DraftRepo | None = None,
        baseline: DraftService | None = None,
    ) -> None:
        self._repo = repo
        self._active_execution = active_execution
        self._drafts = drafts
        self._baseline = baseline

    def _slot_holder(self) -> str | None:
        """进程内仍占执行名额的 Run（S4-03 draining）；未接入探针时为 ``None``。"""
        return None if self._active_execution is None else self._active_execution()

    async def submit_request(
        self,
        *,
        conversation_id: str,
        run_id: str,
        client_request_id: str,
        text: str,
    ) -> dict[str, Any]:
        """提交一次用户请求：``{"created", "run"}``。

        已有活跃 Run 且本次 ``client_request_id`` 不同 → :class:`~storage.errors.ConversationBusy`
        （HTTP 409，不创建 Run 或消息）；相同 ``client_request_id`` 直接返回已有 Run。
        进程内仍在 draining 的取消 Run 与库内活跃 Run 同等触发 busy（08 8.3 名额未释放）。
        """
        return await self._repo.create_run_with_user_message(
            conversation_id,
            run_id,
            client_request_id,
            text,
            active_execution=self._slot_holder(),
        )

    async def regenerate_draft(
        self,
        *,
        parent_draft_id: str,
        run_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        """重新生成草稿（S4-08 Q1=C/Q2=A）：旧草稿身份 → 新 Run，绝不修改旧草稿。

        - **幂等**：已有 Pending 子草稿（按 ``parent_draft_id`` 匹配）时直接返回其来源 Run，
          ``created=False``，不启动第二次执行；子草稿进入终态（已确认／已丢弃）后允许再生成。
          同一 ``client_request_id`` 的重复提交由 ``create_recalc_run`` 在同一事务内先于 busy
          返回已有 Run——子草稿尚未产生时也幂等，不建第二个 Run、不重启执行。
        - **资格**：旧草稿必须仍 Pending 且过期（``base_business_version`` 与当前
          ``context_version`` 不符）；已确认／已丢弃／已是最新的旧草稿拒绝（409
          ``invalid_request``），不新建 Run，也不改动旧草稿。
        - **全局单 Run**：与对话 Run 共用 :meth:`RunRepo.create_recalc_run` 的同事务幂等与 busy
          判定（draining 名额同样计入），不建第二套状态机。

        返回 ``{"created", "run", "parent"}``；``parent`` 供调用方构造执行入口（同一次读取，
        不接受调用方另传旧草稿内容）。
        """
        if self._drafts is None or self._baseline is None:
            raise RuntimeError("RunService 未接入草稿／档案仓库：无法重新生成草稿")
        parent = await self._drafts.get(parent_draft_id)
        if parent is None:
            raise NotFound(f"重新生成的旧草稿不存在: {parent_draft_id}")
        child = await self._drafts.find_pending_child(parent_draft_id)
        if child is not None:
            run = (
                None if child.run_id is None else await self._repo.get_run(child.run_id)
            )
            if run is None:
                raise NotFound(f"重新生成的子草稿缺少来源 Run: {child.id}")
            return {"created": False, "run": run, "parent": parent}
        if parent.status != "pending":
            # 已确认／已丢弃的旧草稿不允许重算（Q2=A）；复用既有 409 ``invalid_request`` 映射
            # （stage2.md 的草稿状态不允许该操作），不新增语义错误码。
            raise DraftNotCorrectable(
                f"仅 Pending 草稿可重新生成: {parent_draft_id}（当前 {parent.status}）"
            )
        current = await self._baseline.prepare_generation_baseline()
        if parent.base_business_version == current.context_version:
            raise DraftNotCorrectable(
                f"旧草稿已基于最新业务版本，无需重新生成: {parent_draft_id}"
            )
        result = await self._repo.create_recalc_run(
            str(parent.conversation_id),
            run_id,
            client_request_id,
            active_execution=self._slot_holder(),
        )
        return {
            "created": bool(result["created"]),
            "run": result["run"],
            "parent": parent,
        }

    async def request_review(
        self, *, run_id: str, client_request_id: str
    ) -> dict[str, Any]:
        """显式复盘生成（S4-08 Q3=B）：创建一个 ``kind='review'`` 的 Agent Run。

        复盘只由用户显式请求触发（无定时、无周任务、无聊天工具入口）：本方法经
        :meth:`RunRepo.create_review_run` 复用同一幂等查重→busy 顺序与全局单 Run 限制，
        不建第二套状态机；生成、幂等先于 busy 与预算／取消一律由执行驱动沿用。
        """
        return await self._repo.create_review_run(
            run_id,
            client_request_id,
            active_execution=self._slot_holder(),
        )

    async def retry_request(
        self,
        *,
        previous_run_id: str,
        run_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        """手动重试：为旧 Run 的同一会话与同一请求文本创建**新** Run。

        ``retry_of_run_id`` 指向旧 Run；本次不复活、不修改旧记录（08 8.1/8.4），
        旧 Run 的框架上下文与历史只读保留。旧 Run 仍在执行时由全局单 Run 判定拒绝
        （``ConversationBusy``），不做断点续跑。
        """
        previous = await self._repo.get_run(previous_run_id)
        if previous is None:
            raise NotFound(f"待重试的 Run 不存在: {previous_run_id}")
        text = await self._repo.get_user_request_text(previous_run_id)
        if text is None:
            raise NotFound(f"待重试的 Run 没有用户请求文本: {previous_run_id}")
        return await self._repo.create_run_with_user_message(
            str(previous["conversation_id"]),
            run_id,
            client_request_id,
            text,
            retry_of_run_id=previous_run_id,
            active_execution=self._slot_holder(),
        )

    async def recover_interrupted_runs(self) -> list[str]:
        """启动期（迁移完成后）把遗留 ``pending``/``running`` 标为失败。

        单事务完成状态与事件写入（08 8.4）；不做断点续跑、不自动重试，由用户手动重试。
        返回被标失败的 Run 身份，便于启动日志给出可核实的数量。
        """
        return await self._repo.fail_interrupted_runs(INTERRUPTED_BY_RESTART)

    async def fail_run(self, *, run_id: str, error_code: str) -> dict[str, Any]:
        """普通执行失败：``running`` → ``failed``，原因只能取普通失败码（08 8.1/8.4）。

        契约校验在这里（``ORDINARY_FAILURE_CODES``）：``interrupted_by_restart`` 属启动恢复专用，
        不接受外部传入；未知码抛 ``InvalidInput``，状态不允许抛 ``RunStateConflict``。
        何时判为失败、如何分类与退避归 S4-03/S4-05；本方法不调度、不重试、不动会话内容。
        """
        if error_code not in ORDINARY_FAILURE_CODES:
            raise InvalidInput(
                f"普通执行失败原因只能是 {sorted(ORDINARY_FAILURE_CODES)}: {error_code!r}"
            )
        return await self._repo.fail_run(run_id, error_code)
