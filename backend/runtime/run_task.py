"""S4-03 执行驱动：单进程单名额、唯一终态、取消与 draining（08 8.1/8.3/8.4）。

职责与边界：

- **名额**：同一时刻只允许一次底层执行。``start`` 在任何 await 之前同步登记名额，请求路径
  以 :attr:`ExecutionDriver.active_run_id` 作为进程内的 busy 事实（库内五状态看不见
  draining）；底层调用实际退出后（执行任务 ``finally``）才释放（08 8.3）。
- **终态唯一**：终态写入仍由 :class:`~storage.run_repo.RunRepo` 的条件写入裁决（07 7.5）；
  cancel/complete、cancel/fail 竞态中落败的一方得到 ``RunStateConflict``，本层视作
  「另一终态已提交」直接丢弃，不重试、不补写、不覆盖。
- **底层执行**：``work`` 由 S4-04/S4-05 提供（模型与工具）。成功返回完整框架消息
  （即 ``complete_run`` 的输入）；可分类失败抛 :class:`ExecutionFailure`（原因码限普通失败码）。
  本层不做错误分类、重试、预算、摘要与 SSE；未分类异常不映射成任何错误码，向上抛出
  （Run 停在 ``running``，由启动恢复收口），不伪造失败原因。
- **取消入口**：只有显式 :meth:`ExecutionDriver.cancel`（08 8.3 仅取消按钮触发）。事件流
  （SSE）只经 :attr:`ExecutionDriver.on_status` 单向观察状态变化，**没有**「观察者触发执行」的
  接缝：断开或刷新既不取消也不重跑，观察者缺席不影响执行。已发生的副作用不回滚、不补写
  「已回滚」痕迹；取消后不写迟到成功消息，也不启动后续模型／工具尝试。
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from runtime.error_codes import ORDINARY_FAILURE_CODES
from storage.errors import InvalidInput, RunStateConflict
from storage.run_repo import RunRepo

#: 底层执行成功时必须给出的完整框架消息 ``(role, payload_json)``，即
#: :meth:`~storage.run_repo.RunRepo.complete_run` 的输入（序列化归 S4-04）。
FrameworkMessages = list[tuple[str, str]]


class ExecutionFailure(Exception):
    """底层执行的可分类失败；``error_code`` 必须取自普通失败码封闭集合（08 8.1/8.4）。

    分类依据（HTTP 状态、``finish_reason``、超时、预算）与重试归 S4-05；本层只把已分类的
    原因码写进唯一终态，不解释、不改写、不为未分类异常发明原因码。
    """

    def __init__(self, error_code: str) -> None:
        if error_code not in ORDINARY_FAILURE_CODES:
            raise InvalidInput(
                f"普通执行失败原因只能是 {sorted(ORDINARY_FAILURE_CODES)}: {error_code!r}"
            )
        super().__init__(error_code)
        self.error_code = error_code


@dataclass
class ActiveExecution:
    """一次占用唯一名额的 Run 执行。

    ``cancel_requested`` 由驱动写入、``work`` 只读：底层调用被取消但无法立即退出时，
    据此拒绝启动后续模型／工具尝试（08 8.3、8.6）。

    ``execution_task`` 是驱动自己持有的执行任务强引用：``asyncio`` 只对任务保持弱引用，
    而生产入口（``api.routes_chat``）不保留 ``start`` 的返回值，引用必须由驱动持有到名额释放，
    否则执行中可能被 GC 而静默停摆（Run 停在 ``running``）。
    """

    run_id: str
    cancel_requested: bool = False
    work_task: asyncio.Task[FrameworkMessages] | None = None
    execution_task: asyncio.Task[None] | None = None


class ExecutionDriver:
    """单进程执行驱动；生产进程内唯一实例（全局单 Run 的进程内部分，08 8.2）。

    ``on_status`` 是 S4-07 的传输观察者（进程内事件流）：每次状态提交成功后拿到库内 Run 行，
    只用于向 SSE 发状态事件；它不是执行入口，抛错也不改变已提交的状态（本层不吞异常，
    观察者自身的缺陷必须暴露）。未接线时行为与 S4-03 完全一致。
    """

    def __init__(
        self,
        repo: RunRepo,
        *,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._repo = repo
        self._on_status = on_status
        self._active: ActiveExecution | None = None

    def _announce(self, run: dict[str, Any]) -> None:
        """状态提交成功后通知传输观察者（不落库、不参与状态判定）。"""
        if self._on_status is not None:
            self._on_status(run)

    @property
    def active_run_id(self) -> str | None:
        """仍占执行名额的 Run（执行中或取消后 draining）；空闲为 ``None``。"""
        return None if self._active is None else self._active.run_id

    def start(
        self,
        run_id: str,
        work: Callable[[ActiveExecution], Coroutine[Any, Any, FrameworkMessages]],
    ) -> asyncio.Task[None]:
        """同步登记名额并启动执行；返回的执行任务可被等待结算（结果在库态，不在返回值）。

        名额被占用时抛 :class:`RuntimeError`：请求路径应先以 ``active_run_id`` 拒绝，
        走到这里是调度缺陷，不静默排队（首版无 Queue，08 8.2）。
        """
        if self._active is not None:
            raise RuntimeError(f"执行名额已被占用: {self._active.run_id}")
        active = ActiveExecution(run_id=run_id)
        self._active = active
        active.execution_task = asyncio.create_task(self._execute(active, work))
        return active.execution_task

    async def cancel(self, run_id: str) -> dict[str, Any]:
        """显式取消：条件持久化 ``cancelled``，再尽力中断当前底层调用（08 8.3）。

        持久化失败（终态不可再流转）抛 :class:`~storage.errors.RunStateConflict`，且不打断
        仍在健康执行的调用；持久化成功后即使底层此刻已产出结果，迟到结果也不会落库
        （提交前的取消标记与条件写入双重把关）。
        """
        run = await self._repo.cancel_run(run_id)
        active = self._active
        if active is not None and active.run_id == run_id:
            active.cancel_requested = True
            if active.work_task is not None:
                # 尽力中断，不等待退出：draining 期间名额继续占用，底层调用退出后才释放。
                active.work_task.cancel()
        self._announce(run)
        return run

    async def _execute(
        self,
        active: ActiveExecution,
        work: Callable[[ActiveExecution], Coroutine[Any, Any, FrameworkMessages]],
    ) -> None:
        try:
            try:
                started = await self._repo.start_run(active.run_id)
            except RunStateConflict:
                return  # 启动前已被取消或已是终态：绝不启动底层调用
            if active.cancel_requested:
                return  # cancel 与 pending→running 竞争且取消已提交：同样不启动
            self._announce(started)
            active.work_task = asyncio.create_task(work(active))
            result = (await asyncio.gather(active.work_task, return_exceptions=True))[0]
            if active.cancel_requested:
                return  # 已取消：迟到的成功、失败与部分结果一律不落库
            if isinstance(result, asyncio.CancelledError):
                raise result  # 底层被外部取消而非用户取消：不伪装终态，上报并保持 running
            if isinstance(result, ExecutionFailure):
                await self._fail(active.run_id, result.error_code)
            elif isinstance(result, BaseException):
                raise result
            else:
                await self._complete(active.run_id, result)
        finally:
            active.execution_task = None  # 与名额释放同步解除强引用
            self._active = None  # 底层调用实际结束后才释放名额（08 8.3）

    async def _complete(self, run_id: str, messages: FrameworkMessages) -> None:
        try:
            run = await self._repo.complete_run(run_id, messages)
        except RunStateConflict:
            return  # 另一终态（取消等）已提交：丢弃迟到成功，不写任何消息（07 7.5）
        self._announce(run)

    async def _fail(self, run_id: str, error_code: str) -> None:
        try:
            run = await self._repo.fail_run(run_id, error_code)
        except RunStateConflict:
            return  # 另一终态已提交：不追加失败事件
        self._announce(run)
