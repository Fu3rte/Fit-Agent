"""Agent Run 的事件流成帧与客户端断连收敛。"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse
from starlette.types import Send

from app.api.dependencies import iso_now
from app.application.agent.contracts import AgentEvent
from app.application.agent.run_service import converge_cancelled_run
from app.application.ports import Conversations


class AgentRunStream(StreamingResponse):
    """客户端断开的收敛覆盖到下游 ``send`` 边界。"""

    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        conversations: Conversations,
        run_id: str,
    ) -> None:
        super().__init__(content, media_type="text/event-stream")
        self._conversations = conversations
        self._run_id = run_id

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        except asyncio.CancelledError:
            await converge_cancelled_run(
                self._conversations, self._run_id, now=iso_now
            )
            raise


def frame(event: AgentEvent) -> str:
    """一条 SSE 帧：事件名 ＋ JSON 数据；五种事件名与载荷键都来自 ``contracts.py`` 的事件契约。"""
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


async def stream_frames(events: AsyncIterator[AgentEvent]) -> AsyncIterator[str]:
    """产品事件流 → SSE 文本帧流：成帧在持久化之后，persist-before-send 次序由此成立。"""
    async for event in events:
        yield frame(event)
