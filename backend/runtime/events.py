"""S4-07：SSE 产品语义事件映射、进程内事件广播与 15 秒 heartbeat（08 8.7、8.8）。

三条硬边界（stage4.md §6 的 SSE 白名单）：

1. **只映射产品事件**：Run 状态、可见回答块、已持久化草稿引用、上下文压缩状态、
   依据与说明结果、heartbeat。框架原始事件、隐藏推理（``ThinkingPart`` 与思考增量）、
   工具调用与调试轨迹、Token／Cache 数值与密钥一律不进事件——事件名集合在
   :data:`EVENT_KINDS` 处封闭，非白名单事件名构造不出来。
2. **广播只服务实时显示**：进程内、不落库、不分配 ID 或恢复游标、不重放。断线或刷新后
   由业务接口查询已保存内容与当前状态（07 7.4、08 8.7）；订阅队列满时丢事件，不阻塞执行。
3. **heartbeat 只是传输保活**：连续 :data:`HEARTBEAT_SECONDS` 秒没有业务事件时发一次，
   不入库、不分配 ID、不显示在聊天里（08 8.7）。

本模块不启动执行、不改 Run 状态、不读业务事实；Run 状态一律以 SQLite 为事实源
（:func:`run_product_events` 每次状态事件都回读库）。
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta

from storage.run_repo import RunRepo

#: 连续多久没有业务事件就发一次 heartbeat（08 8.7 已拍 15 秒；传输层常量，不入库）。
HEARTBEAT_SECONDS = 15.0

#: SSE 产品事件白名单（08 8.7 的事件全集；封闭集合，新增种类须先改契约文档）。
#: ``rationale``（依据与说明结果）**本阶段没有生产者**：有事实支持的来源与简短说明必须由
#: 业务侧给出，不得用编造的说明填充，无内容也不展示空区域（08 8.7/8.8），因此这里只保留
#: 事件种类、不提供构造器。
EVENT_KINDS: tuple[str, ...] = (
    "status",
    "answer",
    "rationale",
    "draft",
    "compression",
    "heartbeat",
)

#: Run 终态（08 8.1）：终态的 status 事件之后事件流结束，不再等待业务事件。
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: 单个订阅者的待发队列上限：慢消费者丢事件（允许丢失，恢复走查询），绝不阻塞执行。
_SUBSCRIBER_QUEUE_SIZE = 256

#: 只允许经总线转发的业务事件（heartbeat 属传输层，不能入库或进广播，08 8.7）。
_BROADCAST_KINDS = frozenset(kind for kind in EVENT_KINDS if kind != "heartbeat")


def _require_event_kind(name: str) -> None:
    if name not in EVENT_KINDS:
        raise ValueError(f"未登记的产品事件：{name!r}；只允许 {EVENT_KINDS}")


def run_status_event(run: Mapping[str, Any]) -> dict[str, Any]:
    """Run 行 → 最小运行状态事件：唯一权威状态 + 可理解的失败原因，不含内部字段。"""
    return {
        "run_id": str(run["id"]),
        "status": str(run["status"]),
        "error_code": run["error_code"],
    }


def answer_event(run_id: str, text: str) -> dict[str, Any]:
    """可见回答块（增量文本）：只含模型产出的可见回答，隐藏推理与工具轨迹不在此列。"""
    return {"run_id": run_id, "text": text}


def draft_event(
    run_id: str, *, draft_id: str, kind: str, revision: int, status: str
) -> dict[str, Any]:
    """已持久化草稿引用（身份 + 修订）：通知在草稿落盘之后发布，不是草稿当前状态的事实源。"""
    return {
        "run_id": run_id,
        "draft_id": draft_id,
        "kind": kind,
        "revision": revision,
        "status": status,
    }


def compression_event(run_id: str, state: str) -> dict[str, Any]:
    """上下文压缩状态：``started``（开始整理）／``finished``（整理结束，含放弃后保留旧上下文）。"""
    if state not in ("started", "finished"):
        raise ValueError(f"压缩状态只能是 started／finished：{state!r}")
    return {"run_id": run_id, "state": state}


def visible_text_delta(event: object) -> str | None:
    """框架流事件 → 可见回答文本增量；不是可见文本的（隐藏推理、工具调用、结果事件等）返回 ``None``。

    PydanticAI 的文本部件首块经 ``PartStartEvent`` 给出（``TextPart`` 自身带首段内容），
    后续增量经 ``PartDeltaEvent`` + ``TextPartDelta``；两者都要取，否则每一段的开头会丢。
    这是「框架事件 → 产品可见内容」的唯一映射点：隐藏推理（``ThinkingPart``）与工具调用
    部件在这里就不是可见文本，因此不可能落到答案事件里（08 8.7/8.8）。
    """
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    return None


class RunEventStream:
    """进程内单 Run 事件广播：只服务实时显示，不落库、不分配游标、不重放（08 8.7）。"""

    def __init__(self) -> None:
        self._subscribers: dict[
            str, set[asyncio.Queue[tuple[str, dict[str, Any]]]]
        ] = {}

    @asynccontextmanager
    async def subscribe(
        self, run_id: str
    ) -> AsyncIterator[asyncio.Queue[tuple[str, dict[str, Any]]]]:
        """订阅一个 Run 的实时事件；退出时注销（断线不取消执行，也不留订阅者）。"""
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_SIZE
        )
        self._subscribers.setdefault(run_id, set()).add(queue)
        try:
            yield queue
        finally:
            subscribers = self._subscribers.get(run_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    del self._subscribers[run_id]

    def publish(self, run_id: str, name: str, payload: dict[str, Any]) -> None:
        """广播一个业务事件；逐订阅者非阻塞投递，队列满即丢（不阻塞执行、不重放）。"""
        if name not in _BROADCAST_KINDS:
            raise ValueError(
                f"不能广播未登记或非业务事件：{name!r}；只允许 {sorted(_BROADCAST_KINDS)}"
            )
        for queue in tuple(self._subscribers.get(run_id, ())):
            try:
                queue.put_nowait((name, payload))
            except asyncio.QueueFull:
                continue

    def publish_status(self, run: Mapping[str, Any]) -> None:
        """Run 状态变化（执行驱动的状态观察者调用；状态本身仍以库内行为准）。"""
        self.publish(str(run["id"]), "status", run_status_event(run))

    def publish_answer(self, run_id: str, text: str) -> None:
        """可见回答块（流式增量；与分批落盘相互独立）。"""
        self.publish(run_id, "answer", answer_event(run_id, text))

    def publish_draft(
        self, run_id: str, *, draft_id: str, kind: str, revision: int, status: str
    ) -> None:
        """草稿就绪通知：只在草稿已持久化之后调用。"""
        self.publish(
            run_id,
            "draft",
            draft_event(
                run_id, draft_id=draft_id, kind=kind, revision=revision, status=status
            ),
        )

    def publish_compression(self, run_id: str, state: str) -> None:
        """上下文压缩状态变化（开始／结束）。"""
        self.publish(run_id, "compression", compression_event(run_id, state))


async def run_product_events(
    *,
    repo: RunRepo,
    events: RunEventStream,
    run_id: str,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """一个 Run 的产品事件流：先给当前库内状态，再转发实时事件，终态即结束。

    - 状态以 SQLite 为事实源（08 8.7）：订阅后立刻读一次并发出，此后每次收到状态事件再回读；
      终态（``completed``／``failed``／``cancelled``）发出后流结束。
    - 连续 ``heartbeat_seconds`` 没有业务事件即产出一个 heartbeat（传输层保活，不入库、
      不分配 ID）；测试用缩小值驱动同一实现，不另建第二套心跳逻辑。
    - 不重放：订阅之前发生的事件不补发，恢复走业务查询（08 8.7）。
    """
    async with events.subscribe(run_id) as queue:
        run = await repo.get_run(run_id)
        if run is None:
            return
        status = str(run["status"])
        yield ("status", run_status_event(run))
        while status not in TERMINAL_RUN_STATUSES:
            try:
                name, payload = await asyncio.wait_for(
                    queue.get(), timeout=heartbeat_seconds
                )
            except TimeoutError:
                yield ("heartbeat", {})
                continue
            if name != "status":
                yield (name, payload)
                continue
            run = await repo.get_run(run_id)
            if run is None:
                return
            status = str(run["status"])
            yield ("status", run_status_event(run))


def sse_frame(name: str, payload: Mapping[str, Any]) -> str:
    """产品事件 → 一帧 SSE 文本：``event:`` + ``data:``。

    **不写 ``id:`` 字段**：``run_events.id`` 只是数据库行身份，不做恢复游标、不做补读
    （07 7.4、08 8.7），因此传输上根本没有可补读的事件 ID。
    """
    _require_event_kind(name)
    return (
        f"event: {name}\n"
        f"data: {json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)}\n\n"
    )
